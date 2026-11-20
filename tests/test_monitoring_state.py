"""Migracao e fronteiras de destinatarios do stop, sem credenciais nem rede."""
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "src"))
from recorder import StreamRecorder,Live
from storage import Store


class StateTests(unittest.TestCase):
    def test_sem_primeiro_bloco_nao_exibe_tempo_infinito(self):
        rec=StreamRecorder(Live('alvo','','',0,''),Path('.'))
        rec._online(True,'123456')
        with patch('recorder.time.monotonic',return_value=10):
            rec._part_started(Path('first/capture.mkv'))
        with patch('recorder.time.monotonic',return_value=10+3600):
            self.assertEqual(rec.progress()['part_seconds'],0)
            self.assertEqual(rec.progress()['recorded_seconds'],0)

    def test_migra_inscricoes_existentes_e_recupera_pausa_e_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bot.sqlite3'
            db=sqlite3.connect(path)
            db.executescript("""CREATE TABLE users(user_id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,blocked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE streamers(username TEXT PRIMARY KEY,platform_id TEXT NOT NULL DEFAULT '');
                CREATE TABLE subscriptions(user_id INTEGER,username TEXT,generation TEXT NOT NULL,PRIMARY KEY(user_id,username));
                INSERT INTO users VALUES(1,10,0); INSERT INTO streamers VALUES('alvo','111');
                INSERT INTO subscriptions VALUES(1,'alvo','old-generation');""")
            db.close()
            store=Store(path)
            self.assertEqual(store.subscription(1,'alvo')['enabled'],1)
            store.stop_live(1,'alvo','123456','a'*32)
            store.set_enabled(1,'alvo',False)
            store.close()
            store=Store(path)
            try:
                self.assertEqual(store.subscriptions(1),['alvo'])
                self.assertEqual(store.subscription(1,'alvo')['stopped_live_id'],'123456')
                self.assertEqual(store.streamers(),[])
                self.assertEqual(store.platform_id('alvo'),'111')
                self.assertEqual(store.recipients('alvo','123456','a'*32),{1:10})
            finally:store.close()

    def test_stop_recebe_so_o_corte_solicitado_mesmo_se_pausar_depois(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'bot.sqlite3')
            try:
                for uid in (1,2):store.add(uid,uid*10,'alvo')
                cut=uuid.uuid4().hex;other=uuid.uuid4().hex;next_live=uuid.uuid4().hex
                store.stop_live(1,'alvo','123456',cut)
                store.set_enabled(1,'alvo',False)
                store.register_clip(cut,'alvo','123456',time.time(),Path(tmp)/'cut.mkv',{},3600)
                store.register_clip(other,'alvo','123456',time.time()+1,Path(tmp)/'other.mkv',{},3600)
                def users(cid):
                    return {r[0] for r in store.db.execute('SELECT user_id FROM clip_recipients WHERE clip_id=?',(cid,))}
                self.assertEqual(users(cut),{1,2});self.assertEqual(users(other),{2})
                store.set_enabled(1,'alvo',True)
                store.stop_live(1,'alvo','123456',cut)
                store.register_clip(next_live,'alvo','234567',time.time()+2,Path(tmp)/'next.mkv',{},3600)
                self.assertEqual(users(next_live),{1,2})
                self.assertEqual(store.pending_clip()['id'],cut)
                store.remove(1,'alvo');store.add(1,10,'alvo')
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM stop_requests').fetchone()[0],0)
            finally:store.close()

    def test_progresso_real_conta_escritas_e_nao_inclui_espera_offline(self):
        rec=StreamRecorder(Live('alvo','','',0,''),Path('.'),part_limit=100)
        rec._online(True,'123456')
        with patch('recorder.time.monotonic',return_value=10):rec._part_started(Path('part-id/capture.mkv'))
        file=io.BytesIO();rec._write(file,b'x'*60)
        with patch('recorder.time.monotonic',return_value=12):
            value=rec.progress();self.assertEqual(value['recorded_seconds'],2)
            rec._part_finished()
        self.assertEqual(value['part_bytes'],len(file.getvalue()))
        with self.assertRaises(RuntimeError):rec._write(file,b'y'*41)
        self.assertEqual(len(file.getvalue()),60)
        with patch('recorder.time.monotonic',return_value=999):self.assertEqual(rec.progress()['recorded_seconds'],2)
        rec._online(True,'234567')
        self.assertEqual(rec.progress()['live_bytes'],0)

    def test_novo_segmento_zera_progresso_sem_apagar_acumulado_compartilhado(self):
        rec=StreamRecorder(Live('alvo','','',0,''),Path('.'))
        rec._online(True,'123456')
        with patch('recorder.time.monotonic',return_value=10):
            rec._part_started(Path('first/capture.mkv'))
        rec._write(io.BytesIO(),b'x'*60)
        with patch('recorder.time.monotonic',return_value=70):
            self.assertEqual(rec.progress()['part_seconds'],60)
            rec._part_finished()
        with patch('recorder.time.monotonic',return_value=80):
            rec._part_started(Path('second/capture.mkv'))
            value=rec.progress()
            self.assertEqual((value['part_bytes'],value['part_seconds']),(0,0))
        rec._write(io.BytesIO(),b'y'*20)
        with patch('recorder.time.monotonic',return_value=82):
            value=rec.progress()
        self.assertEqual((value['part_bytes'],value['part_seconds']),(20,2))
        self.assertEqual((value['live_bytes'],value['recorded_seconds']),(80,62))
