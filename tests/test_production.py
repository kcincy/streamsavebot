"""Admission, overload recovery, durable retention and verified backup restore."""
from dataclasses import replace
from contextlib import closing
import io
import os
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from backups import create_backup,verify_archive
from config import Config
from contact_sheets import initialize
from delivery_service import SpoolBudget
from resource_control import CaptureGate,PlatformBackoff,ResourceProbe
from storage import Store
from traffic_control import TrafficGuard


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config=replace(Config('test'),database=self.root/'database/bot.sqlite3',spool=self.root/'cache/spool',spool_bytes=2_000_000_000,part_bytes=300_000_000)
        self.config.spool.mkdir(parents=True)
        self.store=Store(self.config.database);self.addCleanup(self.store.close)
        self.store.add(1,1,'one');self.store.add(1,1,'two');self.store.add(2,2,'three')
        self.delivery=SimpleNamespace(budget=SpoolBudget(10),backing_up=False)
        self.now=0
        self.gate=CaptureGate(self.config,self.store,self.delivery,clock=lambda:self.now)
        self.good=dict(cpu=.2,memory=.2,free=100*1024**3)
        self.gate.update(self.good)

    def test_stagger_fairness_and_shared_profile(self):
        self.assertTrue(self.gate.request('one',[1]))
        self.gate.starting.clear()
        self.assertFalse(self.gate.request('two',[1]))
        self.assertFalse(self.gate.request('three',[2]))
        self.now=16
        self.assertFalse(self.gate.request('two',[1]))
        self.assertTrue(self.gate.request('three',[2]))
        self.assertTrue(self.gate.request('one',[1,2]))
        self.assertEqual(len(self.gate.active),2)

    def test_pressure_blocks_hysteresis_and_one_victim(self):
        self.gate.request('one',[1]);self.gate.starting.clear();self.now=16;self.gate.request('three',[2])
        self.gate.update(dict(self.good,memory=.91))
        self.now=22;self.gate.update(dict(self.good,memory=.91))
        self.assertEqual(self.gate.revoked,{'three'})
        self.assertFalse(self.gate.request('two',[1]))
        self.now=32;self.gate.update(dict(self.good,memory=.85));self.assertTrue(self.gate.blocked)
        self.now=40;self.gate.update(self.good)
        self.now=69;self.gate.update(self.good);self.assertTrue(self.gate.blocked)
        self.now=70;self.gate.update(self.good);self.assertFalse(self.gate.blocked)
        self.gate.release('three');self.assertNotIn('three',self.gate.active)

    def test_disk_full_metrics_failure_and_pending_budget(self):
        self.gate.update(dict(self.good,free=100))
        self.assertFalse(self.gate.request('one',[1]))
        self.gate.update(self.good);self.delivery.budget.ids=set(range(10))
        self.assertFalse(self.gate.request('one',[1]))
        self.delivery.budget.ids.clear()
        with patch.object(self.gate.probe,'sample',side_effect=OSError):self.gate.poll()
        self.assertFalse(self.gate.request('one',[1]));self.assertEqual(self.gate.reason,'metrics')

    def test_queue_survives_restart_and_cancel(self):
        self.gate.update(dict(self.good,memory=.9));self.gate.request('one',[1])
        other=CaptureGate(self.config,self.store,self.delivery,clock=lambda:self.now)
        self.assertEqual(other.position('one'),1)
        other.cancel('one');self.assertEqual(other.position('one'),0)
        self.assertFalse(list(self.store.db.execute('SELECT * FROM capture_wait')))

    def test_expired_waiter_cannot_hold_queue(self):
        self.gate.update(dict(self.good,memory=.9));self.gate.request('one',[1])
        self.now=121;self.gate.update(self.good)
        self.assertFalse(self.gate.order)

    def test_command_bursts_are_bounded_per_user(self):
        guard=TrafficGuard(self.store,clock=lambda:self.now)
        self.assertTrue(all(guard.allowed(1) for _ in range(20)))
        self.assertFalse(guard.allowed(1));self.assertTrue(guard.allowed(2))
        self.now=1;self.assertTrue(guard.allowed(1));self.assertTrue(guard.allowed(1));self.assertFalse(guard.allowed(1))

    def test_platform_failures_back_off_and_success_resets(self):
        policy=PlatformBackoff()
        for _ in range(5):policy.failed('tiktok')
        self.assertGreater(policy.delay('tiktok',15),15)
        self.assertEqual(policy.delay('kick',15),15)
        policy.recovered('tiktok');self.assertEqual(policy.delay('tiktok',15),15)

    def test_backup_restores_wal_database_pending_video_and_images(self):
        initialize(self.config.database.parent/'sheets')
        image=self.config.database.parent/'sheets/images/test.jpg';image.write_bytes(b'image')
        folder=self.config.spool/('a'*32);folder.mkdir();raw=folder/'capture.flv';raw.write_bytes(b'pending-source')
        self.store.register_clip('a'*32,'one','live',1,raw,{},1)
        with patch('backups.shutil.disk_usage',return_value=SimpleNamespace(free=100*1024**3)):
            archive=create_backup(self.config)
        restored=self.root/'restore';manifest=verify_archive(archive,restored)
        self.assertEqual((restored/'cache/spool'/('a'*32)/'capture.flv').read_bytes(),b'pending-source')
        self.assertEqual((restored/'database/sheets/images/test.jpg').read_bytes(),b'image')
        with closing(sqlite3.connect(restored/'database/bot.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0],3)
        self.assertNotIn('project/.env',manifest['sha256'])
        with self.assertRaises(ValueError):verify_archive(archive,restored)

    def test_backup_failure_preserves_previous_and_rejects_bad_digest(self):
        initialize(self.config.database.parent/'sheets')
        with patch('backups.shutil.disk_usage',return_value=SimpleNamespace(free=100*1024**3)):
            archive=create_backup(self.config)
        with patch('backups.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaises(OSError):create_backup(self.config)
        self.assertTrue(archive.exists())
        bad=self.root/'bad.zip'
        with zipfile.ZipFile(bad,'w') as z:
            z.writestr('x',b'wrong');z.writestr('manifest.json',json.dumps({'sha256':{'x':'0'*64}}))
        with self.assertRaises(ValueError):verify_archive(bad)

    def test_300_mb_spool_slots(self):
        self.assertEqual(replace(self.config,spool_bytes=16384*1024**2).spool_slots,52)

    def test_upload_fairness_keeps_order_within_each_profile(self):
        paths=[]
        for ident,profile,users in [('a','one',[1]),('b','two',[1]),('c','three',[2])]:
            folder=self.config.spool/(ident*32);folder.mkdir();path=folder/'capture.mp4';path.write_bytes(b'video')
            self.store.register_clip(ident*32,profile,'live',1,path,{},3600)
            self.store.processed(ident*32,[(path,'video')],{str(path):{'duration_seconds':30}})
        first=self.store.next_delivery();self.assertEqual(first['user_id'],1)
        self.store.sending(first);self.store.delivery(first,'sent')
        self.assertEqual(self.store.next_delivery()['user_id'],2)

    @unittest.skipIf(os.name=='nt','Linux cgroup counters')
    def test_cpu_is_normalized_to_container_quota_and_cache_is_reclaimable(self):
        group=self.root/'cgroup';group.mkdir()
        for name,value in {'cpu.stat':'usage_usec 0','cpu.max':'600000 100000',
                           'memory.current':'600000000','memory.max':'1000000000',
                           'memory.stat':'inactive_file 100000000'}.items():(group/name).write_text(value)
        probe=ResourceProbe(self.config.spool,group)
        with patch('resource_control.time.monotonic',return_value=100):probe.sample()
        (group/'cpu.stat').write_text('usage_usec 3000000')
        with patch('resource_control.time.monotonic',return_value=101):sample=probe.sample()
        self.assertAlmostEqual(sample['cpu'],3/min(6,len(os.sched_getaffinity(0))))
        self.assertAlmostEqual(sample['memory'],.5)


if __name__=='__main__':unittest.main()
