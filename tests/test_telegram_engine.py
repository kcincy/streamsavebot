"""Captura, segmentacao, FFmpeg e SDK reais; site e respostas Telegram locais."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import recorder as engine
from monitor_service import MonitorRegistry
from test_ctrl_c_windows import HTML


from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from telegram import Update
from telegram.request import HTTPXRequest
from config import Config
from storage import Store
from delivery_service import DeliveryService,media_duration
from telegram_bot import build_application,install_commands


class TelegramEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_captura_unica_segmenta_envia_reutiliza_e_limpa(self):
        await self.exercise(False)

    async def test_stop_envia_e_continua_gravando_apos_perfil_ficar_online(self):
        await self.exercise(True)

    async def test_historical_usage_does_not_interrupt_either_recipient(self):
        await self.exercise(False,quota_mode=True)

    async def test_stop_cuts_twice_and_keeps_the_same_live_player_and_recipients(self):
        await self.exercise(False,send_mode=True)

    async def exercise(self,stop_mode,quota_mode=False,send_mode=False):
        state={'live_id':None if stop_mode else '123456'}
        retrieved=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                raw=HTML.replace('123456',state['live_id']).encode() if '/livestream/' in self.path else b"<script>fetch('/api/web/users/profile',{method:'POST',body:'{}'})</script>"
                self.send(raw,'text/html')
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                value={'user':{'user_id':'111','username':'alvo','livestream_id':state['live_id']}}
                if 'livestream' in self.path:
                    value['stream_details']={'livestream_id':state['live_id'],'channel_id':'target'}
                    retrieved.append(state['live_id'])
                self.send(json.dumps(value).encode(),'application/json')
            def send(self,raw,content_type):
                self.send_response(200);self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            # O limite real de 10s tem teste com FFmpeg; aqui os cortes sao acelerados.
            with tempfile.TemporaryDirectory(prefix='bot-real-engine-') as tmp,patch('resource_control.ResourceProbe.sample',return_value=dict(cpu=.1,memory=.1,free=100*1024**3)),patch('recorder.BASE',f'http://127.0.0.1:{server.server_port}'),patch('recorder.PROFILE_POLL_SECONDS',.05),patch('delivery_service.MIN_VIDEO_SECONDS',.5):
                folder=Path(tmp)
                config=replace(Config('123456:TEST_ONLY_NO_NETWORK_CREDENTIAL'),database=folder/'bot.sqlite3',spool=folder/'spool',api_files=folder/'api-files',segment_seconds=0 if stop_mode or send_mode else 2)
                store=Store(config.database);app=build_application(config)
                pipeline=DeliveryService(config,store,app.bot)
                registry=MonitorRegistry(config,store,pipeline);install_commands(app,registry)
                registry.gate.clock=lambda:time.monotonic()*100
                uploads=[];sent=[];messages=[]
                async def request(request,url,method,request_data=None,**kwargs):
                    endpoint=url.rsplit('/',1)[-1]
                    if endpoint=='getMe':result={'id':123456,'is_bot':True,'first_name':'Test','username':'test_bot'}
                    elif endpoint=='sendMessage':
                        data=request_data.parameters;messages.append(data['text'])
                        result={'message_id':len(messages),'date':1,'chat':{'id':data['chat_id'],'type':'private'},'text':data['text']}
                    elif endpoint in ('sendDocument','sendVideo'):
                        data=request_data.parameters;kind='video' if endpoint=='sendVideo' else 'document'
                        media=data[kind]
                        if str(media).startswith('file:'):
                            path=unquote(urlsplit(media).path)
                            if sys.platform=='win32':path=path.lstrip('/')
                            info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',path]))
                            self.assertEqual({s['codec_type'] for s in info['streams']},{'audio','video'})
                            subprocess.run(['ffmpeg','-v','error','-xerror','-i',path,'-f','null','-'],check=True,capture_output=True)
                            self.assertGreater(media_duration(Path(path)),.5)
                            uploads.append(path);file_id='FILE_'+str(len(uploads))
                        else:
                            self.assertTrue(media.startswith('FILE_'));file_id=media
                        sent.append((data['chat_id'],file_id))
                        obj={'file_id':file_id,'file_unique_id':file_id,'width':384,'height':256,'duration':2}
                        result={'message_id':100+len(sent),'date':1,'chat':{'id':data['chat_id'],'type':'private'},kind:obj}
                    elif endpoint=='answerCallbackQuery':result=True
                    elif endpoint=='editMessageText':
                        data=request_data.parameters
                        result={'message_id':1,'date':1,'chat':{'id':data['chat_id'],'type':'private'},'text':data['text']}
                    else:raise AssertionError(endpoint)
                    return 200,json.dumps({'ok':True,'result':result}).encode()
                counter=0
                async def command(text,user):
                    nonlocal counter
                    for message in text.split(maxsplit=1):
                        counter+=1
                        data={'update_id':counter,'message':{'message_id':counter,'date':1,'from':{'id':user,'is_bot':False,'first_name':'Test'},'chat':{'id':user,'type':'private'},'text':message,'entities':[{'type':'bot_command','offset':0,'length':len(message)}] if message.startswith('/') else []}}
                        await app.process_update(Update.de_json(data,app.bot))
                        if message=='/add':
                            nonce=app.user_data[user]['platform_choice']
                            callback={'update_id':counter+10000,'callback_query':{'id':'choice','chat_instance':'test',
                                'from':{'id':user,'is_bot':False,'first_name':'Test'},'data':f'platform:{nonce}:superlive',
                                'message':{'message_id':counter,'date':1,'chat':{'id':user,'type':'private'},'text':'platform'}}}
                            await app.process_update(Update.de_json(callback,app.bot))
                async def wait_for(predicate):
                    async def wait():
                        while not predicate():
                            for task in pipeline.tasks:
                                if task.done():task.result()
                            await asyncio.sleep(.05)
                    await asyncio.wait_for(wait(),35)
                with patch.object(HTTPXRequest,'do_request',request):
                    await app.initialize();await pipeline.start();await registry.start()
                    try:
                        await command('/start',10);await command('/add @Alvo',10);await command('/add alvo',20)
                        self.assertEqual(len(registry.monitors),1)
                        if stop_mode:
                            monitor=registry.monitors['alvo']
                            await wait_for(lambda:monitor.recorder.progress()['online'] is False)
                            self.assertEqual(len(messages),5,'monitoramento offline enviou mensagem automatica')
                            self.assertEqual(uploads,[])
                            state['live_id']='123456'
                        if send_mode or stop_mode:
                            monitor=registry.monitors['alvo']
                            for index in (1,2):
                                await wait_for(lambda:monitor.recorder.progress()['part_seconds']>1)
                                previous=monitor.recorder.progress()['part_id']
                                await command('/stop alvo',10 if index==1 else 20)
                                await wait_for(lambda:len(sent)>=index*2)
                                self.assertEqual({chat for chat,fid in sent if fid=='FILE_'+str(index)},{10,20})
                                self.assertEqual(store.subscription(10,'alvo')['stopped_live_id'],'')
                                self.assertEqual(store.subscription(20,'alvo')['stopped_live_id'],'')
                                self.assertTrue(store.subscription(10,'alvo')['enabled'])
                                self.assertTrue(store.subscription(20,'alvo')['enabled'])
                                self.assertIn('Recording and monitoring continue.',messages[-1])
                                self.assertFalse(monitor.recorder.stop_event.is_set())
                                await wait_for(lambda:monitor.recorder.progress()['part_id']!=previous and monitor.recorder.progress()['part_bytes']>5000)
                            self.assertEqual(len(retrieved),1)
                            await command('/remove alvo',10);await command('/remove alvo',20)
                            await asyncio.wait_for(monitor.task,20)
                            await wait_for(lambda:not pipeline.budget.ids)
                            return
                        if quota_mode:
                            monitor=registry.monitors['alvo']
                            store.charge_quota(10,'2026-11-19',999999)
                            await wait_for(lambda:len(uploads)>=3 and len(sent)>=6)
                            self.assertEqual(store.recipients('alvo','123456'),{10:10,20:20})
                            self.assertTrue(all({chat for chat,fid in sent if fid=='FILE_'+str(i)}=={10,20} for i in (1,2,3)))
                            self.assertNotIn('Weekly',registry.details(10,10)[0]['text'])
                            await command('/remove alvo',10);await command('/remove alvo',20)
                            await asyncio.wait_for(monitor.task,20)
                            await wait_for(lambda:not pipeline.budget.ids)
                            return
                        await wait_for(lambda:len(uploads)>=2 and len(sent)>=4)
                        self.assertEqual({chat for chat,file_id in sent if file_id=='FILE_1'},{10,20})
                        self.assertEqual(len(retrieved),1,'rotacao abriu outro player')
                        start=asyncio.get_running_loop().time();await command('/monitor',10)
                        self.assertLess(asyncio.get_running_loop().time()-start,1)
                        self.assertIn('<b>alvo (111) · SuperLive</b>\n',messages[-1])
                        monitor=registry.monitors['alvo'];await command('/remove alvo',10)
                        self.assertFalse(monitor.recorder.stop_event.is_set())
                        before=len(sent);await wait_for(lambda:len(sent)>before)
                        self.assertTrue(all(chat==20 for chat,_ in sent[before:]))
                        await command('/remove alvo',20)
                        await asyncio.wait_for(monitor.task,20)
                        await wait_for(lambda:not pipeline.budget.ids)
                    finally:
                        await registry.close();await pipeline.close();await app.shutdown();store.close()
                self.assertEqual(list(config.spool.iterdir()),[])
                self.assertEqual(len(retrieved),1)
        finally:
            server.shutdown();server.server_close();thread.join()
