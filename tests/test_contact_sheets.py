"""FFmpeg real: amostras uniformes, grade sem adornos e catalogo apos limpeza."""
import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import contact_sheets
from config import Config
from delivery_service import DeliveryService,run_media,prepare_videos
from project_runtime import media_tool
from storage import Store

COLORS=[(220,30,30),(30,220,30),(30,30,220),(220,220,30),(220,30,220),
        (30,220,220),(120,40,40),(40,120,40),(40,40,120)]


def video(folder,colors=COLORS):
    raw=folder/'colors.rgb'
    rate=10 if len(colors)>1 else 1
    raw.write_bytes(b''.join(bytes(color)*80*48*rate for color in colors))
    path=folder/'colors.mp4'
    run_media([media_tool('ffmpeg'),'-v','error','-nostdin','-y','-f','rawvideo',
        '-pixel_format','rgb24','-video_size','80x48','-framerate',str(rate),'-i',str(raw),
        '-c:v','libx264','-threads','1','-g','1','-pix_fmt','yuv420p',str(path)])
    return path


def record(ident='a'*32):
    return dict(recording_id=ident,live_id='live-1',part_number=1,title='Titulo',
        profile_name='alvo',profile_url='https://stripchat.com/alvo',profile_id='111',platform='stripchat',
        recording_started_at='2026-11-19T10:00:00+00:00',recording_ended_at='2026-11-19T10:00:09+00:00',
        telegram_user_ids='[1, 2, 9876543210]')


class ContactSheetTests(unittest.TestCase):
    def test_nove_cores_uniformes_sem_espacamento_e_idempotencia(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=contact_sheets.initialize(root/'database'/'sheets')
            source=video(root)
            # Duas saidas de envio continuam formando uma unica folha cronologica.
            first=root/'first.mp4';last=root/'last.mp4'
            for path,start,duration in ((first,0,4),(last,4,5)):
                run_media([media_tool('ffmpeg'),'-v','error','-nostdin','-y','-ss',str(start),
                    '-i',str(source),'-t',str(duration),'-c','copy',str(path)])
            outputs,info=prepare_videos([(first,'video'),(last,'video')],500_000_000)
            for _ in range(2):contact_sheets.create_sheet(folder,record(),outputs,info,run_media)
            sheet=folder/'images'/('a'*32+'.jpg')
            probe=json.loads(run_media([media_tool('ffprobe'),'-v','error','-show_streams','-of','json',str(sheet)]))
            self.assertEqual((probe['streams'][0]['width'],probe['streams'][0]['height']),(1440,810))
            pixels=run_media([media_tool('ffmpeg'),'-v','error','-i',str(sheet),'-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
            for index,color in enumerate(COLORS):
                for x,y in ((20,20),(240,135),(459,249)):
                    offset=((index//3*270+y)*1440+index%3*480+x)*3
                    actual=tuple(pixels[offset:offset+3])
                    self.assertTrue(all(abs(a-b)<15 for a,b in zip(actual,color)),(index,actual,color))
            with closing(sqlite3.connect(folder/'sheets.sqlite3')) as db:
                db.row_factory=sqlite3.Row;rows=db.execute('SELECT * FROM recordings').fetchall()
                self.assertEqual(len(rows),1);row=rows[0]
                self.assertEqual(row['duration_seconds'],9)
                self.assertEqual(row['video_file_count'],2)
                self.assertEqual(json.loads(row['telegram_user_ids']),[1,2,9876543210])
                self.assertEqual(row['sheet_filename'],sheet.name)
                self.assertEqual(row['status'],'ready')
            self.assertEqual(list((folder/'images').iterdir()),[sheet])

    def test_retrato_quadrado_e_panoramico_recortam_centro_sem_faixas(self):
        for width,height in ((80,144),(96,96),(160,48)):
            with self.subTest(resolution=(width,height)),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);folder=contact_sheets.initialize(root/'sheets')
                raw=root/'frame.rgb';source=root/'source.mp4'
                crop_width=min(width,height*16/9);crop_height=min(height,width*9/16)
                raw.write_bytes(b''.join(bytes((30,220,30) if
                    abs(x-width/2)<crop_width/2+2 and abs(y-height/2)<crop_height/2+2
                    else (220,30,30)) for y in range(height) for x in range(width)))
                run_media([media_tool('ffmpeg'),'-v','error','-nostdin','-y','-f','rawvideo',
                    '-pixel_format','rgb24','-video_size',f'{width}x{height}','-framerate','1',
                    '-i',str(raw),'-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(source)])
                outputs,info=prepare_videos([(source,'video')],500_000_000)
                contact_sheets.create_sheet(folder,record(),outputs,info,run_media)
                sheet=folder/'images'/('a'*32+'.jpg')
                probe=json.loads(run_media([media_tool('ffprobe'),'-v','error','-show_streams','-of','json',str(sheet)]))
                self.assertEqual((probe['streams'][0]['width'],probe['streams'][0]['height']),(1440,810))
                pixels=run_media([media_tool('ffmpeg'),'-v','error','-i',str(sheet),'-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
                for index in range(9):
                    for x,y in ((20,20),(240,135),(459,249)):
                        offset=((index//3*270+y)*1440+index%3*480+x)*3
                        self.assertTrue(all(abs(a-b)<15 for a,b in zip(pixels[offset:offset+3],(30,220,30))))

    def test_um_frame_preenche_as_nove_celulas_e_erro_fica_registrado(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=contact_sheets.initialize(root/'sheets');source=video(root,COLORS[:1])
            outputs,info=prepare_videos([(source,'video')],500_000_000)
            contact_sheets.create_sheet(folder,record(),outputs,info,run_media)
            def failure(args):raise RuntimeError('synthetic failure')
            with self.assertRaises(RuntimeError):
                contact_sheets.create_sheet(folder,record('b'*32),outputs,info,failure)
            with closing(sqlite3.connect(folder/'sheets.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT status,error FROM recordings WHERE recording_id=?',('b'*32,)).fetchone(),
                                 ('failed','RuntimeError'))
            self.assertEqual(len(list((folder/'images').iterdir())),1)


class ContactSheetPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalogo_contem_participantes_e_persiste_apos_limpeza(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            config=replace(Config('TEST'),database=root/'database'/'bot.sqlite3',spool=root/'spool',api_files=root/'files')
            store=Store(config.database)
            bot=SimpleNamespace(send_video=AsyncMock(),send_document=AsyncMock())
            pipeline=DeliveryService(config,store,bot)
            try:
                for uid in (1,2,9876543210):store.add(uid,uid,'stripchat:alvo')
                store.fix_identity('stripchat:alvo','111')
                ident='c'*32;folder=pipeline._folder(ident);folder.mkdir()
                raw=folder/'capture.mp4';source=await asyncio.to_thread(video,root)
                shutil.copyfile(source,raw)
                store.begin_capture('stripchat:alvo',ident,'live-1')
                store.leave_capture(2,'stripchat:alvo',ident)
                started=time.time()-9
                store.register_clip(ident,'stripchat:alvo','live-1',started,raw,
                    dict(title='Minha live',profile_id='111',ended=started+9,seconds=9),3600)
                await pipeline.start()
                async def completed():
                    while store.db.execute('SELECT status FROM clips WHERE id=?',(ident,)).fetchone()[0]!='done':
                        await asyncio.sleep(.05)
                await asyncio.wait_for(completed(),30)
                self.assertFalse(folder.exists());bot.send_video.assert_not_called()
                with closing(sqlite3.connect(pipeline.sheets/'sheets.sqlite3')) as db:
                    db.row_factory=sqlite3.Row;row=db.execute('SELECT * FROM recordings').fetchone()
                    self.assertEqual(row['title'],'Minha live')
                    self.assertEqual(row['profile_id'],'111')
                    self.assertEqual(row['profile_url'],'https://stripchat.com/alvo')
                    self.assertEqual(json.loads(row['telegram_user_ids']),[1,2,9876543210])
                    self.assertTrue((pipeline.sheets/'images'/row['sheet_filename']).is_file())
                    self.assertTrue(row['recording_ended_at'].endswith('+00:00'))
            finally:
                await pipeline.close();store.close()
