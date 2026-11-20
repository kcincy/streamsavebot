"""Layout, migracao da fila e MP4 finalizado sem alterar os pacotes de audio/video."""
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from config import Config
from delivery_service import prepare_videos,probe_video,run_media,process_recoverably
from project_runtime import ROOT,CACHE,DATABASE,media_tool
from storage import Store,relocate_media_paths


def atoms(path):
    result=[]
    with path.open('rb') as file:
        while header:=file.read(8):
            size,kind=struct.unpack('>I4s',header)
            header_size=8
            if size==1:
                size=struct.unpack('>Q',file.read(8))[0];header_size=16
            result.append(kind)
            if size==0:
                break
            file.seek(size-header_size,1)
    return result


class LayoutTests(unittest.TestCase):
    def test_codigo_e_dados_separados(self):
        self.assertEqual(Path(__import__('storage').__file__).parent,ROOT/'src')
        self.assertEqual(Config('test').database,DATABASE/'bot.sqlite3')
        self.assertEqual(Config('test').spool,CACHE/'spool')
        self.assertTrue((ROOT/'main.py').is_file())

    def test_move_fila_sem_modificar_usuarios_estados_ou_destinatarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);before=root/'old';after=root/'cache'/'spool'
            before.mkdir();after.parent.mkdir()
            old=root/'old.sqlite3';store=Store(old)
            store.add(1,10,'alvo');store.add(2,20,'alvo');store.set_enabled(2,'alvo',False)
            folder=before/('a'*32);folder.mkdir();raw=folder/'capture.mkv';raw.write_bytes(b'recoverable')
            store.register_clip('a'*32,'alvo','123456',time.time(),raw,{},3600)
            output=folder/'media.mp4';output.write_bytes(b'complete')
            store.processed('a'*32,[(output,'video')])
            subscriptions=[tuple(r) for r in store.db.execute('SELECT * FROM subscriptions ORDER BY user_id')]
            deliveries=[tuple(r) for r in store.db.execute('SELECT * FROM deliveries')]
            store.close()
            dbroot=root/'database';dbroot.mkdir()
            old.rename(dbroot/'bot.sqlite3')
            before.rename(after)
            store=Store(dbroot/'bot.sqlite3')
            try:
                relocate_media_paths(store.db,before,after)
                self.assertEqual([tuple(r) for r in store.db.execute('SELECT * FROM subscriptions ORDER BY user_id')],subscriptions)
                self.assertEqual([tuple(r) for r in store.db.execute('SELECT * FROM deliveries')],deliveries)
                row=store.next_delivery()
                self.assertEqual(row['user_id'],1)
                self.assertEqual(Path(row['path']).read_bytes(),b'complete')
                self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            finally:store.close()


class CompatibilityTests(unittest.TestCase):
    def make_clip(self,folder):
        source=folder/'interrupted-style.mp4'
        run_media([media_tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i',
            'testsrc2=size=160x120:rate=25:duration=3','-f','lavfi','-i','sine=duration=3',
            '-c:v','libx264','-threads','2','-preset','ultrafast','-c:a','aac',
            '-movflags','+frag_keyframe+empty_moov','-frag_duration','1000000',str(source)])
        return source

    def test_faststart_duracao_e_pacotes_preservados(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=self.make_clip(Path(tmp))
            self.assertIn(b'moof',atoms(source))
            outputs,info=prepare_videos([(source,'video')],500000000)
            final=outputs[0][0]
            self.assertNotEqual(final,source)
            self.assertTrue(source.exists(),'origem deve ficar disponivel ate confirmar envio')
            top=atoms(final)
            self.assertNotIn(b'moof',top)
            self.assertLess(top.index(b'moov'),top.index(b'mdat'))
            self.assertGreaterEqual(info[str(final)]['duration'],3)
            self.assertEqual(info[str(final)]['width'],160)
            def digest(path):
                return run_media([media_tool('ffmpeg'),'-v','error','-i',str(path),'-map','0',
                                  '-c','copy','-f','hash','-hash','sha256','-'])
            self.assertEqual(digest(source),digest(final),'remux deve preservar o conteudo codificado')
            run_media([media_tool('ffmpeg'),'-v','error','-xerror','-i',str(final),'-f','null','-'])

    def test_faststart_incompleto_preserva_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=self.make_clip(Path(tmp))
            outputs,info=prepare_videos([(source,'video')],10000)
            self.assertEqual(outputs[0][0],source)
            self.assertTrue(source.exists())
            self.assertFalse(source.with_name(source.stem+'.ready.mp4').exists())

    def test_recuperacao_nao_altera_arquivo_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=self.make_clip(Path(tmp));original=source.read_bytes()
            row={'id':'a'*32,'raw_path':str(source),'metadata':json.dumps({'mime':'video/mp4;codecs=avc1,aac'}),
                 'username':'alvo','live_id':'123456'}
            with patch('delivery_service.process_clip',side_effect=[RuntimeError('unexpected_end'),[(Path(tmp)/'done.mp4','video')]]):
                result=process_recoverably(row,500000000,Path(tmp),4*1024**3)
            self.assertEqual(source.read_bytes(),original)
            self.assertTrue(source.with_name('recovered.mkv').exists())
