"""Site local com recomendacoes, tres reinicios e tentativa de troca de conta."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recorder import Live, StreamRecorder


HTML = r"""<!doctype html><body><script>
(async () => {
  const post = (url, data) => fetch(url, {method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(data)}).then(r=>r.json());
  if (!location.pathname.includes('/livestream/')) {
    await post('/api/web/users/profile', {username:'alvo'}); return;
  }
  const id = location.pathname.split('/').pop();
  const data = await post('/api/web/livestream/retrieve', {livestream_id:id});
  const ac = new AudioContext(); await ac.resume();
  function player(lid, channel, width, freq) {
    const canvas = document.createElement('canvas'); canvas.width=width; canvas.height=240;
    const ctx=canvas.getContext('2d'); let n=0;
    setInterval(()=>{ctx.fillStyle=`hsl(${n++%360},80%,50%)`;ctx.fillRect(0,0,width,240)},33);
    const dest=ac.createMediaStreamDestination(),osc=ac.createOscillator();
    osc.frequency.value=freq;osc.connect(dest);osc.start();
    const stream=canvas.captureStream(30); stream.addTrack(dest.stream.getAudioTracks()[0]);
    const root=document.createElement('div');root.id='teleport-video-'+lid;
    const video=document.createElement('video');video.dataset.pipChannel=channel;
    video.srcObject=stream;video.muted=true;root.append(video);document.body.append(root);video.play();
    return video;
  }
  const target=player(id,data.stream_details.channel_id,320,440);
  player('999999','recommendation',640,880);
  setTimeout(async()=>{
    await post('/fixture/drop',{});
    target.dataset.pipChannel='another-account';
    post('/api/web/livestream/retrieve',{livestream_id:'999999'}).catch(()=>{});
  },2600);
})().catch(e=>console.error(e));
</script>"""


class FollowIntegrationTests(unittest.TestCase):
    def test_reinicios_e_recomendacoes_com_navegador_e_ffmpeg_reais(self):
        self.run_reconnects(False)

    def test_perfil_por_id_sem_username_grava_reconecta_e_bloqueia_outra_conta(self):
        self.run_reconnects(True)

    def run_reconnects(self,anonymous):
        state = {"slot": 0, "offline": False, "retrieved": []}
        ids = ["123456", "234567", "345678", "456789"]
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, value, content_type="application/json"):
                raw = value.encode() if isinstance(value, str) else json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.send(HTML, "text/html")

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                with lock:
                    slot = state["slot"]
                    uid = "111" if slot < 4 else "222"
                    lid = ids[min(slot, 3)]
                    if self.path == "/fixture/drop":
                        state["slot"] += 1
                        state["offline"] = True
                        self.send({"ok": True})
                    elif self.path == "/api/web/users/profile":
                        offline = state["offline"]
                        state["offline"] = False
                        self.send({"user": {"user_id": uid, "username": None if anonymous else "alvo",
                                            "livestream_id": None if offline else lid}})
                    elif self.path == "/api/web/livestream/retrieve":
                        requested = data["livestream_id"]
                        state["retrieved"].append(requested)
                        self.send({"user": {"user_id": uid, "username": None if anonymous else "alvo"},
                                   "stream_details": {"livestream_id": requested,
                                                      "channel_id": "channel-" + requested}})
                    else:
                        self.send({})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="follow-integration-") as tmp, \
                    patch("recorder.BASE", f"http://127.0.0.1:{server.server_port}"), \
                    patch("recorder.PROFILE_POLL_SECONDS", 0.05):
                rec = StreamRecorder(Live("" if anonymous else "alvo", "", "", 0, "",profile_id='111' if anonymous else ''),
                                     Path(tmp),duration=30,stop_event=threading.Event())
                result = rec.run()
                self.assertEqual(rec.target_user_id, "111")
                self.assertIn("outra conta", result.error)
                self.assertEqual(state["retrieved"], ids)
                self.assertEqual(len(result.paths), 4)
                self.assertEqual(len(set(result.paths)), 4)
                for index,output in enumerate(result.paths):
                    stem=ids[index] if anonymous else 'alvo'
                    self.assertRegex(output.name, r'^'+stem+r'_\d{2}\.\d{2}(?:_\d+)?\.mp4$')
                    self.assertRegex(output.parent.name, r"^\d{2}\.\d{2}\.\d{4}$")
                    self.assertEqual(output.parent.parent.name,stem)
                    info = json.loads(subprocess.check_output([
                        "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output),
                    ]))
                    video = next(s for s in info["streams"] if s["codec_type"] == "video")
                    self.assertEqual((video["width"], video["height"]), (320, 240))
                    self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
