"""fMP4, Chromium, FFmpeg e fila reais; site local e entregas controladas."""
import asyncio
from contextlib import closing
from dataclasses import replace
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import sqlite3
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from config import Config
from delivery_service import DeliveryService
from monitor_service import MonitorRegistry
from project_runtime import media_tool
from recorder import Live
from storage import Store
from stripchat_capture import SegmentWriter
from stripchat_recorder import Session,StripchatRecorder


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_resposta_de_outro_perfil_nao_confunde_sequencia_com_id_do_alvo(self):
        session=Session(None,{'username':'alvo','id':111,'stream_name':'111','recordable':True})
        session.responses=SimpleNamespace(submit=Mock())
        session.response(SimpleNamespace(url='https://cdn/x_111_source_222.mp4',status=200))
        session.responses.submit.assert_not_called()
        session.response(SimpleNamespace(url='https://cdn/x_12_source_111.mp4',status=200))
        session.responses.submit.assert_called_once()

    async def test_erro_ao_fechar_bruto_ainda_registra_prefixo_para_recuperacao(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);owner=StripchatRecorder(Live('alvo','','',0,''),folder,part_limit=5000)
            outputs=[];session=Session(owner,{'username':'alvo','id':111,'stream_name':'111','recordable':True})
            owner._recording_base=lambda:folder/'capture'
            owner.segment_callback=lambda path,meta:outputs.append(path)
            await session.new_writer()
            session.writer.write_init(b'init');session.writer.add((1,-1),b'media');session.writer.flush(True)
            with patch.object(session.writer,'close',side_effect=OSError('disk')):
                with self.assertLogs('stripchat_recorder',level='WARNING'):await session.publish()
            self.assertEqual(outputs[0].read_bytes(),b'initmedia')
            self.assertFalse(owner.progress()['recording'])

    async def test_reordena_full_substitui_partes_e_respeita_limite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.mp4';writer=SegmentWriter(path,100)
            writer.write_init(b'init')
            writer.add((2,0),b'partial');writer.add((1,-1),b'first');writer.flush(True)
            writer.add((2,-1),b'complete');writer.close()
            self.assertEqual(path.read_bytes(),b'initfirstcomplete')
            writer=SegmentWriter(Path(tmp)/'limited.mp4',10)
            writer.write_init(b'init')
            with self.assertRaises(RuntimeError):writer.add((1,0),b'x'*7)
            writer.close();self.assertLessEqual(writer.path.stat().st_size,10)

    async def test_init_diferente_fecha_parte_antiga_e_contadores_recomecam(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);outputs=[];counter=0
            owner=StripchatRecorder(Live('alvo','','',0,''),folder,part_limit=1000,segment_bytes=900)
            owner.live.livestream_id='123456'
            def base():
                nonlocal counter
                counter+=1;directory=folder/str(counter);directory.mkdir();return directory/'capture'
            owner._recording_base=base
            owner.segment_callback=lambda path,meta:outputs.append((path,meta))
            owner.capture_allowed=lambda live_id:True
            session=Session(owner,{'username':'alvo','id':111,'stream_name':'111','recordable':True})
            session.confirmed=True
            session.page=SimpleNamespace(url='https://pt.stripchat.com/alvo',is_closed=lambda:False)
            init=b'\0\0\0\x10ftypavc1';segment=b'\0\0\0\x10moofdata'
            await session.accept('https://cdn/x_init_111.mp4',init)
            await session.accept('https://cdn/x_1_source_111.mp4',segment)
            await session.accept('https://cdn/x_init_111.mp4',init+b'new')
            self.assertEqual(len(outputs),1)
            await session.accept('https://cdn/x_2_source_111.mp4',segment)
            self.assertEqual(owner.progress()['part_bytes'],len(init)+3)
            await session.publish()
            self.assertEqual([p.read_bytes() for p,_ in outputs],[init+segment,init+b'new'+segment])
            self.assertTrue(all(m['username']=='stripchat:alvo' for _,m in outputs))
            await session.accept('https://cdn/x_3_source_111.mp4',segment)
            await session.accept('https://cdn/x_init_111.mp4',b'\0\0\0\x10ftypencv')
            self.assertEqual(outputs[-1][0].read_bytes(),init+b'new'+segment)
            self.assertTrue(session.encrypted)


class StripchatEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_media_recorder_finaliza_cada_container_antes_de_fechar_navegador(self):
        from playwright.async_api import async_playwright
        from recorder import launch_args
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);outputs=[];counter=0
            owner=StripchatRecorder(Live('alvo','','',0,''),folder,part_limit=500_000_000)
            owner.live.livestream_id='123456'
            def base():
                nonlocal counter
                counter+=1;directory=folder/str(counter);directory.mkdir();return directory/'capture'
            owner._recording_base=base;owner.segment_callback=lambda path,meta:outputs.append((path,meta))
            owner.capture_allowed=lambda live_id:True
            html="""<video autoplay muted></video><script>
                const c=document.createElement('canvas');c.width=160;c.height=120;
                const ctx=c.getContext('2d');setInterval(()=>{ctx.fillStyle='rgb('+Math.floor(Math.random()*255)+',0,0)';ctx.fillRect(0,0,160,120);},100);
                const s=c.captureStream(10);const a=new AudioContext();const osc=a.createOscillator();
                const dst=a.createMediaStreamDestination();osc.connect(dst);osc.start();
                s.addTrack(dst.stream.getAudioTracks()[0]);document.querySelector('video').srcObject=s;
            </script>"""
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(channel='chromium',headless=True,args=launch_args(False))
                try:
                    context=await browser.new_context();page=await context.new_page()
                    await page.route('**/*',lambda route:route.fulfill(body=html,content_type='text/html'))
                    await page.goto('https://pt.stripchat.com/alvo')
                    session=Session(owner,{'username':'alvo','id':111,'stream_name':'111','recordable':True})
                    session.page=page;session.confirmed=True
                    session.refresh_access=AsyncMock(return_value=True)
                    for _ in range(2):
                        await session.start_recorder()
                        async def data_ready():
                            while owner.progress()['part_bytes']<=1000:
                                self.assertFalse(await page.evaluate('window.__scError'))
                                await asyncio.sleep(.1)
                        await asyncio.wait_for(data_ready(),10)
                        await session.stop_recorder();await session.publish()
                    self.assertEqual(len(outputs),2)
                    for path,meta in outputs:
                        self.assertGreater(path.stat().st_size,1000)
                        self.assertTrue(meta['mime'].startswith('video/'))
                        subprocess.run([media_tool('ffmpeg'),'-v','error','-xerror','-i',str(path),'-f','null','-'],check=True,capture_output=True)
                finally:await browser.close()

    async def test_http_direto_sem_navegador_com_ffmpeg_fila_stop_privada_e_nova_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);source=folder/'source.mp4'
            subprocess.run([media_tool('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin',
                '-f','lavfi','-i','testsrc2=size=160x120:rate=10','-f','lavfi','-i','sine=frequency=440',
                '-t','30','-c:v','libx264','-threads','2','-preset','ultrafast','-g','10','-c:a','aac',
                '-movflags','+frag_keyframe+empty_moov+default_base_moof','-frag_duration','1000000',str(source)],check=True)
            raw=source.read_bytes();init=b'';segments=[];offset=0
            while offset<len(raw):
                size,kind=struct.unpack('>I4s',raw[offset:offset+8])
                box=raw[offset:offset+size];offset+=size
                if kind in (b'ftyp',b'moov'):init+=box
                elif kind==b'moof':segments.append(box)
                elif kind==b'mdat':segments[-1]+=box
            state={'live':False,'private':False};epoch=time.monotonic()
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    if self.path.split('?',1)[0].endswith('.m3u8'):
                        sequence=max(1,int((time.monotonic()-epoch)/.4))
                        playlist=('#EXTM3U\n#EXT-X-TARGETDURATION:1\n'
                            '#EXT-X-MOUFLON:PSCH:v2:OLzu7QlySkG2fVRn\n'
                            f'#EXT-X-MEDIA-SEQUENCE:{sequence}\n#EXT-X-MAP:URI="x_init_111.mp4"\n'
                            +''.join(f'#EXTINF:1,\n#EXT-X-MOUFLON:URI:http://{self.headers["Host"]}/hls/111/x_{n}_Ykz+0DiX_111.mp4\nmedia.mp4\n'
                                     for n in range(sequence,sequence+3)))
                        data,content=playlist.encode(),'application/vnd.apple.mpegurl'
                    elif '_init_' in self.path:data,content=init,'video/mp4'
                    elif '/x_' in self.path:
                        if '_source_' not in self.path:
                            self.send_error(400);return
                        index=int(self.path.split('/x_')[1].split('_')[0])
                        data,content=segments[(index-1)%len(segments)],'video/mp4'
                    else:
                        status='private' if state['private'] else 'public' if state['live'] else 'off'
                        boot=json.dumps({'viewCamBase':{'model':{'id':111,'username':'alvo','streamName':'111','isLive':state['live'],'status':status,'topic':'Live de teste'}}})
                        script="""let n=1;fetch('/x_init_111.mp4').then(()=>setInterval(()=>fetch('/x_'+(n++)+'_source_111.mp4'),400));"""
                        data,content=(f'<script>window.boot={boot};{script}</script>').encode(),'text/html'
                    self.send_response(200);self.send_header('Content-Type',content)
                    self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            config=replace(Config('TEST'),database=folder/'bot.sqlite3',spool=folder/'spool',api_files=folder/'files')
            store=Store(config.database);sent=[];uploads=[]
            async def send(**kwargs):
                media=kwargs.get('video',kwargs.get('document'))
                if isinstance(media,Path):
                    self.assertLessEqual(media.stat().st_size,500_000_000)
                    subprocess.run([media_tool('ffmpeg'),'-v','error','-xerror','-i',str(media),'-f','null','-'],check=True,capture_output=True)
                    uploads.append(media);file_id='FILE_'+str(len(uploads))
                else:file_id=media
                sent.append((kwargs['chat_id'],file_id,kwargs['caption']))
                return SimpleNamespace(message_id=len(sent),video=SimpleNamespace(file_id=file_id),document=SimpleNamespace(file_id=file_id))
            bot=SimpleNamespace(send_video=send,send_document=send)
            pipeline=DeliveryService(config,store,bot);registry=MonitorRegistry(config,store,pipeline)
            registry.gate.clock=lambda:time.monotonic()*100
            async def wait(predicate):
                async def check():
                    while not predicate():
                        for task in pipeline.tasks:
                            if task.done():task.result()
                        await asyncio.sleep(.05)
                await asyncio.wait_for(check(),35)
            try:
                # O limite real de 10s tem teste com FFmpeg; aqui os cortes sao acelerados.
                with patch('resource_control.ResourceProbe.sample',return_value=dict(cpu=.1,memory=.1,free=100*1024**3)),patch('stripchat_capture.BASE',f'http://127.0.0.1:{server.server_port}'),patch('stripchat_recorder.BASE',f'http://127.0.0.1:{server.server_port}'),patch('stripchat_recorder.POLL_SECONDS',.1),patch('delivery_service.MIN_VIDEO_SECONDS',.5),patch('stripchat_capture.hls_urls',return_value=[f'http://127.0.0.1:{server.server_port}/hls/111/master/111_auto.m3u8']):
                    registry.browsers.endpoint=AsyncMock(side_effect=AssertionError('HTTP direto não deve abrir navegador'))
                    await pipeline.start();await registry.start()
                    registry.add(1,10,'alvo','stripchat');registry.add(2,20,'alvo','stripchat')
                    monitor=registry.monitors['stripchat:alvo']
                    self.assertIsNone(monitor.recorder.browser_endpoint_provider)
                    await wait(lambda:monitor.recorder.progress()['online'] is False)
                    self.assertEqual(uploads,[]);state['live']=True
                    await wait(lambda:monitor.recorder.progress()['part_bytes']>10000)
                    live_id=monitor.recorder.progress()['live_id']
                    registry.stop(1,10,'stripchat:alvo')
                    await wait(lambda:len(sent)>=2)
                    self.assertEqual({chat for chat,fid,_ in sent if fid=='FILE_1'},{10,20})
                    self.assertEqual(len(uploads),1)
                    self.assertTrue(all('StripChat' in caption for _,_,caption in sent))
                    self.assertTrue(all('<code>@alvo</code>' in caption and 'Profile ID: 111' in caption
                                        for _,_,caption in sent))
                    await wait(lambda:monitor.recorder.progress()['part_bytes']>10000 and monitor.recorder.progress()['part_id']!=store.db.execute('SELECT id FROM clips ORDER BY started LIMIT 1').fetchone()[0])
                    state['private']=True
                    await wait(lambda:monitor.recorder.progress().get('access')=='private' and not monitor.recorder.progress()['recording'])
                    await wait(lambda:len(sent)>=3 and not pipeline.budget.ids)
                    self.assertIn('Private live',registry.details(2,20)[0]['text'])
                    self.assertFalse(registry.details(2,20)[0]['can_stop'])
                    self.assertEqual(monitor.recorder.progress()['live_id'],live_id)
                    before=len(uploads);await asyncio.sleep(.8)
                    self.assertEqual(len(uploads),before)
                    self.assertFalse(monitor.recorder.progress()['recording'])
                    state['private']=False
                    await wait(lambda:monitor.recorder.progress()['recording'] and monitor.recorder.progress()['part_bytes']>10000)
                    registry.stop(2,20,'stripchat:alvo')
                    await wait(lambda:len(sent)>=4 and monitor.recorder.status=='watching')
                    self.assertEqual([chat for chat,fid,_ in sent if fid=='FILE_2'],[20])
                    self.assertEqual([chat for chat,fid,_ in sent if fid=='FILE_3'],[20])
                    state['live']=False
                    await wait(lambda:monitor.recorder.progress()['online'] is False)
                    state['live']=True
                    await wait(lambda:monitor.recorder.progress()['live_id']!=live_id and monitor.recorder.progress()['part_bytes']>10000)
                    registry.stop(1,10,'stripchat:alvo');registry.stop(2,20,'stripchat:alvo')
                    await wait(lambda:len(sent)>=6 and not pipeline.budget.ids)
                    self.assertEqual({chat for chat,fid,_ in sent if fid=='FILE_4'},{10,20})
                    self.assertEqual(store.platform_id('stripchat:alvo'),'111')
                    registry.browsers.endpoint.assert_not_awaited()
            finally:
                await registry.close();await pipeline.close();store.close()
                server.shutdown();server.server_close();thread.join()
            self.assertEqual(list(config.spool.iterdir()),[])
