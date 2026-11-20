"""Promessas que nunca resolvem no Chromium nao podem prender a captura ou o stop."""
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from recorder import Live,StreamRecorder,media_eval,open_context,sync_playwright,PWTimeout
from delivery_service import run_media
from project_runtime import media_tool
from test_ctrl_c_windows import HTML


class CaptureTimeoutTests(unittest.TestCase):
    def test_prazo_do_driver_interrompe_promessa_e_aceita_resultado_vazio(self):
        with sync_playwright() as pw:
            browser,ctx=open_context(pw,False,None)
            try:
                page=ctx.new_page()
                self.assertEqual(media_eval(page,"''"),'')
                started=time.monotonic()
                with self.assertRaises(PWTimeout):
                    media_eval(page,'new Promise(() => {})',timeout=200)
                self.assertLess(time.monotonic()-started,3)
                self.assertEqual(media_eval(page,'42'),42)
            finally:ctx.close();browser.close()

    def test_stop_e_estatisticas_travados_preservam_prefixo_e_permitem_nova_captura(self):
        state={'mode':'stop'}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                inject={
                    'stop':"window.__slRec.stop=()=>new Promise(()=>{});",
                    'stats':"const stats=window.__slRec.videoStats;window.__slRec.videoStats=()=>window.__slRec.bytes ? new Promise(()=>{}) : stats();",
                    'healthy':'',
                }[state['mode']]
                self.send(HTML.replace('await video.play();','await video.play();'+inject).encode(),'text/html')
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                self.send(json.dumps({'user':{'user_id':'111','username':'alvo'},
                    'stream_details':{'livestream_id':'123456','channel_id':'target'}}).encode(),'application/json')
            def send(self,data,kind):
                self.send_response(200);self.send_header('Content-Type',kind)
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
        def bounded(page,expression,arg=None):return media_eval(page,expression,arg,timeout=1000)
        try:
            with tempfile.TemporaryDirectory() as tmp,patch('recorder.BASE',f'http://127.0.0.1:{server.server_port}'),patch('recorder.media_eval',side_effect=bounded),sync_playwright() as pw:
                browser,ctx=open_context(pw,False,None)
                try:
                    for mode in ('stop','stats'):
                        with self.subTest(mode=mode):
                            state['mode']=mode;folder=Path(tmp)/mode;folder.mkdir()
                            published=[]
                            stop=threading.Event()
                            def ready(path,meta):
                                self.assertFalse(rec.progress()['recording'])
                                published.append((path,meta))
                            rec=StreamRecorder(Live('alvo','123456','',0,'',user_id='111'),folder,
                                               stop_event=stop,segment_callback=ready)
                            def signal(terminate=False):
                                until=time.monotonic()+15
                                while time.monotonic()<until and not stop.is_set():
                                    value=rec.progress()
                                    if value['recording'] and value['part_bytes']>2048:
                                        if terminate:stop.set()
                                        else:rec.request_part()
                                        return
                                    time.sleep(.02)
                                stop.set()
                            watcher=None
                            if mode=='stop':
                                watcher=threading.Thread(target=signal);watcher.start()
                            started=time.monotonic()
                            _,reason=rec._record_segment(ctx,1)
                            if watcher:watcher.join(16);self.assertFalse(watcher.is_alive())
                            self.assertEqual(reason,'timeout')
                            self.assertLess(time.monotonic()-started,15)
                            self.assertEqual(len(published),1)
                            prefix=published[0][0]
                            self.assertGreater(prefix.stat().st_size,2048)
                            self.assertFalse(rec.progress()['recording'])
                            self.assertEqual(rec.progress()['part_seconds'],0)
                            run_media([media_tool('ffmpeg'),'-v','error','-xerror','-i',str(prefix),'-f','null','-'])
                            state['mode']='healthy'
                            watcher=threading.Thread(target=signal,args=(True,));watcher.start()
                            _,reason=rec._record_segment(ctx,2)
                            watcher.join(16);self.assertFalse(watcher.is_alive())
                            self.assertEqual(reason,'stopped')
                            self.assertEqual(len(published),2)
                            self.assertNotEqual(prefix,published[1][0])
                            self.assertTrue(prefix.exists(),'a retomada nao pode apagar o prefixo anterior')
                            self.assertLess(published[1][1]['seconds'],10)
                finally:ctx.close();browser.close()
        finally:server.shutdown();server.server_close();server_thread.join()
