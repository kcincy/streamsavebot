"""Ctrl+C real em console Windows isolado, durante captura e durante FFmpeg."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
HTML = r"""<!doctype html><script>
(async()=>{
  await fetch('/api/web/livestream/retrieve',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({livestream_id:'123456'})});
  const canvas=document.createElement('canvas');canvas.width=384;canvas.height=256;
  const ctx=canvas.getContext('2d');let n=0;
  setInterval(()=>{ctx.fillStyle=`hsl(${n++%360},80%,50%)`;ctx.fillRect(0,0,384,256)},40);
  const ac=new AudioContext();await ac.resume();
  const osc=ac.createOscillator(),dest=ac.createMediaStreamDestination();
  osc.connect(dest);osc.start();const stream=canvas.captureStream(25);
  stream.addTrack(dest.stream.getAudioTracks()[0]);
  const root=document.createElement('div');root.id='teleport-video-123456';
  const video=document.createElement('video');video.dataset.pipChannel='target';
  video.srcObject=stream;video.muted=true;root.append(video);document.body.append(root);
  await video.play();
})();</script>"""

CHILD = r"""
import os,sys
sys.path.insert(0,os.path.join(sys.argv[1],"src"))
import recorder as m
m.BASE=sys.argv[2]
mode=sys.argv[4]
opened=m.open_context
state={}
def context(*args,**kwargs):
    browser,ctx=opened(*args,**kwargs);state['browser']=browser
    close_ctx,close_browser=ctx.close,browser.close
    def context_close():
        print('CONTEXT_CLOSE',flush=True);close_ctx()
    def browser_close():
        close_browser();print('BROWSER_CLOSE',flush=True)
    ctx.close=context_close;browser.close=browser_close
    return browser,ctx
m.open_context=context
ffmpeg=m.StreamRecorder._ffmpeg
def finalize(self,args):
    assert state['browser'].is_connected(),'browser fechado antes do FFmpeg'
    if mode=='ffmpeg':args=['-re',*args]
    ok=ffmpeg(self,args)
    print('FFMPEG_DONE '+str(ok),flush=True)
    return ok
m.StreamRecorder._ffmpeg=finalize
popen=m.subprocess.Popen
def spawn(*args,**kwargs):
    proc=popen(*args,**kwargs)
    if os.path.basename(args[0][0]).lower().startswith('ffmpeg'):
        print('FFMPEG_PROCESS_STARTED',flush=True)
    return proc
m.subprocess.Popen=spawn
import logging,signal,threading
from pathlib import Path
logging.basicConfig(level=logging.INFO,format='%(message)s',stream=sys.stdout)
def stop(*_):
    print('Ctrl+C: finalizando',flush=True);m.STOP.set()
signal.signal(signal.SIGINT,stop)
rec=m.StreamRecorder(m.Live('','123456','',0,''),Path(sys.argv[3]),duration=5 if mode=='ffmpeg' else 0)
result=[]
worker=threading.Thread(target=lambda:result.append(rec.run()))
worker.start();worker.join()
sys.exit(0 if result[0].ok else 1)
"""

SENDER = r"""
import ctypes,sys,time
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.FreeConsole()
assert k.AttachConsole(int(sys.argv[1])),ctypes.get_last_error()
k.SetConsoleCtrlHandler(None,True)
assert k.GenerateConsoleCtrlEvent(0,0),ctypes.get_last_error()
time.sleep(.2)
k.FreeConsole()
"""


@unittest.skipUnless(os.name == "nt", "exige console Windows")
class WindowsCtrlCTests(unittest.TestCase):
    def exercise(self, mode):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, raw, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.send(HTML.encode(), "text/html")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send(json.dumps({
                    "stream_details": {"livestream_id": "123456", "channel_id": "target"},
                    "user": {"user_id": "111", "username": "alvo"},
                }).encode(), "application/json")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="superlive-ctrl-c-") as tmp:
                root = Path(tmp)
                out = root / "recordings"
                logfile = root / "console.log"
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                with logfile.open("w", encoding="utf-8") as output:
                    proc = subprocess.Popen([
                        sys.executable, "-B", "-u", "-c", CHILD, str(ROOT),
                        f"http://127.0.0.1:{server.server_port}", str(out), mode,
                    ], stdout=output, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
                        env={**os.environ, "PYTHONUTF8": "1"})
                    try:
                        marker = "FFMPEG_PROCESS_STARTED" if mode == "ffmpeg" else "gravados,"
                        deadline = time.monotonic() + 35
                        while marker not in logfile.read_text(encoding="utf-8"):
                            self.assertIsNone(proc.poll(), logfile.read_text(encoding="utf-8"))
                            self.assertLess(time.monotonic(), deadline, logfile.read_text(encoding="utf-8"))
                            time.sleep(.05)
                        if mode == "capture":
                            time.sleep(2)
                        subprocess.run([sys.executable, "-B", "-c", SENDER, str(proc.pid)],
                                       creationflags=subprocess.CREATE_NO_WINDOW, check=True, timeout=5)
                        proc.wait(timeout=30)
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                            proc.wait()
                logs = logfile.read_text(encoding="utf-8")
                self.assertEqual(proc.returncode, 0, logs)
                self.assertIn("Ctrl+C: finalizando", logs)
                self.assertNotIn("FAIL", logs)
                self.assertNotIn("TargetClosedError", logs)
                self.assertIn("FFMPEG_DONE True", logs)
                self.assertLess(logs.index("FFMPEG_DONE"), logs.index("CONTEXT_CLOSE"))
                self.assertLess(logs.index("CONTEXT_CLOSE"), logs.index("BROWSER_CLOSE"))
                files = list(out.rglob("*.mp4"))
                self.assertEqual(len(files), 1, logs)
                self.assertFalse(list(out.rglob("*.part*.mkv")), logs)
                self.assertFalse(list(out.rglob("*.lock")), logs)
                probe = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(files[0]),
                ]))
                self.assertEqual({s["codec_type"] for s in probe["streams"]}, {"video", "audio"})
                self.assertGreater(float(probe["format"]["duration"]), 1.5)
                subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(files[0]),
                                "-fps_mode", "passthrough", "-f", "null", "-"],
                               check=True, capture_output=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_ctrl_c_durante_captura_salva_mp4_antes_de_fechar_browser(self):
        self.exercise("capture")

    def test_ctrl_c_durante_ffmpeg_aguarda_o_processo(self):
        self.exercise("ffmpeg")


if __name__ == "__main__":
    unittest.main()
