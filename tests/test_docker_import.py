"""Migração de banco/fila para Linux sem sobrescrever dados nem mudar destinatários."""
import importlib.util
from contextlib import closing
from pathlib import Path,PureWindowsPath
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from storage import Store
import contact_sheets

spec=importlib.util.spec_from_file_location('docker_import',ROOT/'scripts'/'docker_import.py')
migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)


class DockerImportTests(unittest.TestCase):
    def test_copia_banco_midia_e_sessao_sem_alterar_origem_ou_destinatarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source';target=Path(tmp)/'container'
            store=Store(source/'database'/'bot.sqlite3')
            store.add(1,10,'stripchat:alvo');store.charge_quota(1,'2026-11-18',123)
            clip='a'*32;folder=source/'cache'/'spool'/clip;folder.mkdir(parents=True)
            raw=folder/'capture.ts';raw.write_bytes(b'recoverable')
            store.register_clip(clip,'stripchat:alvo','live',100,raw,{},3600)
            output=folder/'media.mp4';output.write_bytes(b'processed')
            store.processed(clip,[(output,'video')])
            old=str(PureWindowsPath(r'C:\Users\user\Desktop\old-project\cache\spool')/clip)
            with store.db:
                store.db.execute('UPDATE clips SET raw_path=?',(old+r'\capture.ts',))
                store.db.execute('UPDATE media SET path=?',(f'/old/cache/spool/{clip}/media.mp4',))
            expected=[tuple(row) for row in store.db.execute('SELECT * FROM deliveries')]
            store.close()
            (source/'sessions').mkdir()
            (source/'sessions'/'superlive.json').write_text('{"cookies":[]}',encoding='utf8')
            (source/'settings.env').write_text('SUPERLIVE_STATE=sessions\\superlive.json\n',encoding='utf8')
            sheets=contact_sheets.initialize(source/'database'/'sheets')
            (sheets/'images'/'example.jpg').write_bytes(b'sheet')
            # O Docker cria o destino do bind mount antes de executar o importador.
            (target/'database'/'sheets'/'images').mkdir(parents=True)
            before=(source/'database'/'bot.sqlite3').read_bytes()
            self.assertEqual(migration.import_runtime(source,target),2)
            copied=Store(target/'database'/'bot.sqlite3')
            try:
                self.assertEqual([tuple(row) for row in copied.db.execute('SELECT * FROM deliveries')],expected)
                self.assertEqual(copied.quota_used(1,'2026-11-18'),123)
                row=copied.db.execute('SELECT raw_path FROM clips').fetchone()
                self.assertEqual(Path(row[0]).read_bytes(),b'recoverable')
                self.assertEqual(Path(copied.next_delivery()['path']).read_bytes(),b'processed')
            finally:copied.close()
            self.assertEqual((source/'database'/'bot.sqlite3').read_bytes(),before)
            self.assertEqual((target/'database'/'sessions'/'superlive.json').read_text(),'{"cookies":[]}')
            self.assertEqual((target/'database'/'sheets'/'images'/'example.jpg').read_bytes(),b'sheet')
            with closing(sqlite3.connect(target/'database'/'sheets'/'sheets.sqlite3')) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            with self.assertRaisesRegex(ValueError,'já contêm'):migration.import_runtime(source,target)

    def test_recusa_imagens_locais_existentes_antes_de_importar(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source';target=Path(tmp)/'target'
            store=Store(source/'database'/'bot.sqlite3');store.close()
            images=target/'database'/'sheets'/'images';images.mkdir(parents=True)
            (images/'existing.jpg').write_bytes(b'preserve')
            with self.assertRaises(ValueError):migration.import_runtime(source,target)
            self.assertEqual((images/'existing.jpg').read_bytes(),b'preserve')
            self.assertFalse((target/'database'/'bot.sqlite3').exists())

    def test_recusa_travessia_e_sessao_fora_do_projeto(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source';target=Path(tmp)/'target'
            store=Store(source/'database'/'bot.sqlite3')
            store.add(1,10,'alvo');clip='b'*32
            store.register_clip(clip,'alvo','live',1,Path(r'C:\old\cache\spool')/clip/'..'/'..'/'outside.ts',{},3600)
            store.close()
            with self.assertRaisesRegex(ValueError,'fora do spool'):migration.import_runtime(source,target)
            self.assertFalse((target/'database'/'bot.sqlite3').exists())
            with closing(sqlite3.connect(source/'database'/'bot.sqlite3')) as db:
                db.execute('DELETE FROM clips');db.commit()
            (source/'settings.env').write_text('SUPERLIVE_STATE=../outside.json\n',encoding='utf8')
            with self.assertRaisesRegex(ValueError,'dentro do projeto'):migration.import_runtime(source,target)
            self.assertFalse((target/'database'/'bot.sqlite3').exists())
