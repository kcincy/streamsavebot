"""Admission control: one capture per profile, measured inside the Linux container."""
from collections import Counter
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time

log=logging.getLogger(__name__)


class PlatformBackoff:
    def __init__(self):self.states={};self.lock=threading.Lock()
    def failed(self,platform):
        with self.lock:
            count,until=self.states.get(platform,(0,0));count+=1
            self.states[platform]=(count,time.monotonic()+min(300,30*2**min(4,max(0,count-5))))
    def recovered(self,platform):
        with self.lock:self.states.pop(platform,None)
    def delay(self,platform,normal):
        with self.lock:count,until=self.states.get(platform,(0,0))
        return max(normal,until-time.monotonic()) if count>=5 else normal


class ResourceProbe:
    def __init__(self,folder,cgroup=Path('/sys/fs/cgroup'),host_folders=()):
        self.folder,self.cgroup=Path(folder),cgroup
        self.host_folders=host_folders
        self.previous=None

    def sample(self):
        now=time.monotonic();cpu=0.0;memory=0.0
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            idle,kernel,user=wintypes.FILETIME(),wintypes.FILETIME(),wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle),ctypes.byref(kernel),ctypes.byref(user)):
                raise OSError('cpu_metrics_unavailable')
            def ticks(value):return value.dwHighDateTime*2**32+value.dwLowDateTime
            total=ticks(kernel)+ticks(user);idle_ticks=ticks(idle)
            if self.previous and total>self.previous[0]:cpu=1-(idle_ticks-self.previous[1])/(total-self.previous[0])
            self.previous=(total,idle_ticks)
            class Memory(ctypes.Structure):
                _fields_=[('length',wintypes.DWORD),('load',wintypes.DWORD)]+[(name,ctypes.c_ulonglong) for name in
                    ('total_phys','available_phys','total_page','available_page','total_virtual','available_virtual','extended')]
            value=Memory();value.length=ctypes.sizeof(value)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):raise OSError('memory_metrics_unavailable')
            memory=1-value.available_phys/value.total_phys
        else:
            stat=dict(line.split() for line in (self.cgroup/'cpu.stat').read_text().splitlines())
            usage=int(stat['usage_usec'])/1e6
            quota,period=(self.cgroup/'cpu.max').read_text().split()
            cores=min(len(os.sched_getaffinity(0)),float(quota)/int(period)) if quota!='max' else len(os.sched_getaffinity(0))
            if self.previous:
                elapsed=now-self.previous[0]
                if elapsed>0:cpu=max(0,(usage-self.previous[1])/elapsed/cores)
            self.previous=(now,usage)
            used=int((self.cgroup/'memory.current').read_text())
            limit=(self.cgroup/'memory.max').read_text().strip()
            if limit=='max':raise OSError('memory_limit_required')
            statistics=dict(line.split() for line in (self.cgroup/'memory.stat').read_text().splitlines())
            memory=max(0,used-int(statistics.get('inactive_file',0)))/int(limit)
            meminfo={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()}
            if meminfo.get('MemAvailable',2**63)<512*1024*1024:memory=max(memory,.95)
        free=min(shutil.disk_usage(path).free for path in (self.folder,*self.host_folders) if path.exists())
        return dict(cpu=cpu,memory=memory,free=free)


class CaptureGate:
    def __init__(self,config,store,delivery,clock=time.monotonic):
        self.config,self.store,self.delivery,self.clock=config,store,delivery,clock
        self.probe=ResourceProbe(config.spool,host_folders=(config.database.parent/'backups',config.database.parent/'sheets/images'))
        self.active={};self.revoked=set();self.cooldown={};self.seen={}
        self.starting=set()
        self.blocked=False;self.reason='starting';self.high=None;self.low=None;self.critical=None
        self.last_start=-1e9;self.last_shed=-1e9;self.sample=None
        self.checks=threading.BoundedSemaphore(2)
        store.db.execute('CREATE TABLE IF NOT EXISTS capture_wait(profile TEXT PRIMARY KEY,entered REAL NOT NULL)')
        store.db.commit()
        self.order={r[0]:r[1] for r in store.db.execute('SELECT profile,entered FROM capture_wait')}
        self.seen={key:clock() for key in self.order}

    @contextmanager
    def checking(self,stop):
        while self.sample is not None and self.sample['memory']>=.9:
            if stop.wait(.2):raise InterruptedError()
        while not self.checks.acquire(timeout=.2):
            if stop.is_set():raise InterruptedError()
        try:
            if stop.is_set():raise InterruptedError()
            yield
        finally:self.checks.release()

    def update(self,sample):
        now=self.clock();self.sample=sample
        high=sample['cpu']>=.8 or sample['memory']>=.8
        low=sample['cpu']<.65 and sample['memory']<.7
        self.high=(self.high if self.high is not None else now) if high else None
        self.low=(self.low if self.low is not None else now) if low else None
        critical=sample['memory']>=.9 or sample['cpu']>=.95
        self.critical=(self.critical if self.critical is not None else now) if critical else None
        if self.high is not None and now-self.high>=15:self.blocked=True
        if self.low is not None and now-self.low>=30:self.blocked=False
        self.reason='memory' if sample['memory']>=.8 else 'cpu' if sample['cpu']>=.8 else 'recovery' if self.blocked else ''
        if sample['free']<max(1024**3,4*self.config.part_bytes):self.reason='disk'
        if self.reason=='disk':
            for key in self.active:
                self.revoked.add(key);self.cooldown[key]=now+60
        delay=5 if sample['memory']>=.9 else 60
        if self.critical is not None and now-self.critical>=delay and now-self.last_shed>=30:
            candidates=[key for key in self.active if key not in self.revoked]
            if candidates:
                victim=max(candidates,key=lambda key:self.active[key][0])
                self.revoked.add(victim);self.cooldown[victim]=now+60;self.last_shed=now
                log.warning('resource_suspend profile=%s reason=%s',victim,self.reason)
        for key in list(self.order):
            if now-self.seen.get(key,now)>120:self.cancel(key)

    def poll(self):
        try:self.update(self.probe.sample())
        except (OSError,ValueError,KeyError,ZeroDivisionError):
            self.sample=None;self.reason='metrics';self.blocked=True

    def request(self,key,users):
        if key in self.active:return key not in self.revoked
        now=self.clock();self.seen[key]=now
        if key not in self.order:
            self.order[key]=time.time()
            with self.store.db:self.store.db.execute('INSERT OR IGNORE INTO capture_wait VALUES(?,?)',(key,self.order[key]))
        if (self.delivery.backing_up or self.sample is None or self.blocked or self.reason=='disk' or self.sample['memory']>=.8
                or self.sample['cpu']>=.8 or len(self.active)>=self.config.max_captures
                or self.starting or now-self.last_start<15 or now<self.cooldown.get(key,0)
                or len(self.delivery.budget.ids)>=self.delivery.budget.slots):return False
        eligible=[p for p in self.waiting() if now>=self.cooldown.get(p,0) and self.store.recipients(p)]
        if not eligible or eligible[0]!=key:return False
        self.cancel(key);self.active[key]=(now,set(users));self.starting.add(key);self.last_start=now
        log.info('capture_admitted profile=%s active=%s',key,len(self.active))
        return True

    def cancel(self,key):
        self.order.pop(key,None);self.seen.pop(key,None)
        with self.store.db:self.store.db.execute('DELETE FROM capture_wait WHERE profile=?',(key,))

    def release(self,key):
        self.active.pop(key,None);self.revoked.discard(key);self.starting.discard(key)

    def position(self,key):
        keys=self.waiting()
        return keys.index(key)+1 if key in keys else 0

    def waiting(self):
        counts=Counter(uid for _,owners in self.active.values() for uid in owners)
        return sorted(self.order,key=lambda p:(min((counts[uid] for uid in self.store.recipients(p)),default=10**9),self.order[p],p))

    def health(self):
        return dict(at=time.time(),active=len(self.active),waiting=len(self.order),reason=self.reason,
                    blocked=self.blocked,resources=self.sample,buffer_used=len(self.delivery.budget.ids),
                    buffer_limit=self.delivery.budget.slots,starting=len(self.starting))

    def save_health(self,path):
        path=Path(path);temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.health()),encoding='utf-8');temporary.replace(path)
