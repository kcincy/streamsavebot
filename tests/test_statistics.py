"""Historico por usuario sem reler videos apagados, sem duplicar midia compartilhada."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from storage import Store


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name);self.store=Store(self.folder/'bot.sqlite3');self.addCleanup(self.store.close)

    def clip(self,profile,users,durations,sizes,live='live',legacy=False):
        for uid in users:self.store.add(uid,uid*10,profile)
        clip_id=uuid.uuid4().hex
        raw=self.folder/(clip_id+'.raw');raw.write_bytes(b'x')
        self.store.register_clip(clip_id,profile,live,100+len(list(self.folder.iterdir())),raw,
                                 {'seconds':sum(durations)},3600)
        outputs=[];info={}
        for index,seconds in enumerate(durations):
            path=self.folder/f'{clip_id}-{index}.mp4';path.write_bytes(b'x');outputs.append((path,'video'))
            if not legacy:info[str(path)]={'duration_seconds':seconds}
        self.store.processed(clip_id,outputs,info)
        with self.store.db:
            for index,size in enumerate(sizes):self.store.db.execute('UPDATE media SET size=? WHERE id=?',(size,f'{clip_id}:{index}'))
        raw.unlink()
        for path,_ in outputs:path.unlink()
        return clip_id

    def test_totais_ranking_isolamento_e_historico_apos_remocao(self):
        one=self.clip('alvo',(1,2),(1800,1800),(500_000_000,250_000_000))
        two=self.clip('stripchat:alvo',(1,),(7200,),(300_000_000,))
        self.clip('secreto',(2,),(9000,),(999_000_000,))
        old=self.clip('alvo',(1,),(30,90),(1_000_000,3_000_000),live='old',legacy=True)
        short=self.clip('alvo',(1,),(5,),(100,),live='short')
        with self.store.db:
            self.store.db.execute("UPDATE deliveries SET status='sent'")
            self.store.db.execute("UPDATE deliveries SET status='pending' WHERE media_id=? AND user_id=1",(f'{one}:1',))
            self.store.db.execute("UPDATE deliveries SET status='skipped' WHERE media_id=?",(f'{short}:0',))
        data=self.store.statistics(1)
        self.assertEqual(data['seconds'],10920)
        self.assertEqual(data['bytes'],1_054_000_000)
        self.assertEqual((data['sent_seconds'],data['sent_bytes'],data['received']),(9120,804_000_000,4))
        self.assertEqual((data['lives'],data['profiles'],data['pending'],data['longest']),(3,2,1,7200))
        self.assertEqual(data['top'],[('stripchat:alvo',7200),('alvo',3720)])
        self.assertEqual(data['estimated'],1)
        self.assertNotIn('secreto',str(data))
        self.assertEqual(self.store.statistics(2)['top'][0],('secreto',9000))
        self.store.remove(1,'alvo')
        self.assertEqual(self.store.statistics(1)['seconds'],10920)
        self.assertEqual(self.store.statistics(1)['registered'],1)

    def test_usuario_sem_historico_ve_zeros(self):
        data=self.store.statistics(9)
        self.assertEqual((data['seconds'],data['bytes'],data['received'],data['pending']),(0,0,0,0))
        self.assertEqual(data['top'],[]);self.assertIsNone(data['first']);self.assertIsNone(data['last'])
