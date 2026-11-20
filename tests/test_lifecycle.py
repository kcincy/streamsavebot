"""Regressoes de identidade da live e video parado com audio chegando."""
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recorder import Live, StreamRecorder


def payload(lid):
    return {"stream_details": {"livestream_id": lid, "channel_id": "channel-target"},
            "user": {"user_id": "111", "username": "alvo"}}


class LifecycleTests(unittest.TestCase):
    def test_id_original_nao_e_substituido(self):
        rec = StreamRecorder(Live("", "123456", "", 0, ""), Path("."))
        data=payload('123456');data['stream_details']['title']='Minha live'
        self.assertTrue(rec._accept_live(data))
        self.assertEqual(rec.live.title,'Minha live')
        self.assertFalse(rec._accept_live(payload("999999")))
        self.assertEqual(rec.live.livestream_id, "123456")
        self.assertTrue(rec._live_ended)

    def test_primeiro_id_e_vinculado_ao_usuario(self):
        rec = StreamRecorder(Live("alvo", "", "", 0, ""), Path("."))
        data = payload("123456")
        data["user"] = {"username": "outro"}
        self.assertFalse(rec._accept_live(data))
        self.assertEqual(rec.live.livestream_id, "")

    def test_id_correto_mas_live_encerrada_e_rejeitado(self):
        rec = StreamRecorder(Live("", "155424664", "", 0, ""), Path("."))
        data = payload("155424664")
        data["stream_details"]["finished_at"] = 1794769149012
        self.assertFalse(rec._accept_live(data))
        self.assertTrue(rec._live_ended)
        self.assertFalse(rec._live_confirmed)

    def test_disallowed_finished_sem_timestamp_e_rejeitado(self):
        rec = StreamRecorder(Live("", "155424664", "", 0, ""), Path("."))
        data = payload("155424664")
        data["disallowed_reason"] = "finished"
        self.assertFalse(rec._accept_live(data))

    def test_api_sem_identidade_nao_e_aceita(self):
        for data in ({}, {"error": "unauthorized"}, {"stream_details": []}):
            with self.subTest(data=data):
                rec = StreamRecorder(Live("", "155424664", "", 0, ""), Path("."))
                self.assertFalse(rec._accept_live(data))

    def test_video_antes_da_confirmacao_nao_inicia(self):
        rec = StreamRecorder(Live("", "155424664", "", 0, ""), Path("."))
        page = Mock()
        page.evaluate.return_value = True
        clock = [1000.0]
        def tick(ms):
            clock[0] += ms / 1000
        page.wait_for_timeout.side_effect = tick
        with patch("recorder.time.time", side_effect=lambda: clock[0]):
            self.assertFalse(rec._wait_video(page, 1))
        page.evaluate.assert_not_called()

    def test_id_aguarda_video_antes_de_iniciar_a_duracao(self):
        rec = StreamRecorder(Live("", "123456", "", 0, ""), Path("."), duration=1)
        rec._live_confirmed = True
        clock = [1000.0]
        page = Mock()
        page.evaluate.side_effect = lambda script: clock[0] >= 1002
        page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
        with patch("recorder.time.time", side_effect=lambda: clock[0]):
            self.assertTrue(rec._wait_video(page, 4))

    def test_id_igual_nao_dispensa_conferencia_do_usuario(self):
        rec = StreamRecorder(Live("alvo", "123456", "", 0, ""), Path("."))
        data = payload("123456")
        data["user"] = {"username": "outro"}
        self.assertFalse(rec._accept_live(data))

    def test_url_com_id_ja_inicia_vinculada(self):
        rec = StreamRecorder(Live("", "", "", 0, "",
                             url_override="https://superlivechat.tv/pt/livestream/123456?x=1"), Path("."))
        self.assertEqual(rec.live.livestream_id, "123456")
        self.assertFalse(rec._accept_live(payload("999999")))

    def test_url_com_usuario_nao_aceita_outro_usuario(self):
        rec = StreamRecorder(Live("", "", "", 0, "",
                             url_override="https://superlivechat.tv/pt/alvo"), Path("."))
        data = payload("123456")
        data["user"] = {"username": "outro"}
        self.assertFalse(rec._accept_live(data))

    def record(self, out, switch=False, preload=False, navigate=False, final_only=False):
        clock = [1000.0]
        rec = StreamRecorder(Live("", "123456", "", 0, ""), out, stall_timeout=2)
        page = Mock()
        page.url = rec.live.url
        page.is_closed.return_value = False
        page.route.side_effect = lambda pattern, handler: setattr(page, "guard" if 'retrieve' in pattern else 'navigation_guard', handler)
        aborted = Mock()

        def evaluate(script, *args):
            if '__slRec.start(' in script:
                return {"ok": True, "mimeType": "", "hasAudio": True}
            if 'videoStats()' in script:
                return {'frames':10,'width':160,'height':120}
            if 'blob:' in script:
                if final_only:rec.request_part()
                if navigate:
                    route=Mock()
                    route.request.is_navigation_request.return_value=True
                    route.request.frame=page.main_frame
                    route.request.url=rec.live.url.replace('123456','999999')
                    page.navigation_guard(route)
                    route.abort.assert_called_once();route.fallback.assert_not_called()
                if switch or preload:
                    route = Mock()
                    route.request.post_data_json = {"livestream_id": "999999" if preload else "123456"}
                    route.fetch.return_value = SimpleNamespace(
                        status=200, json=lambda: payload("999999"))
                    route.abort = aborted
                    page.guard(route)
                    route.fulfill.assert_not_called()
                    if preload:
                        route.fetch.assert_not_called()
                return {"blob": '' if final_only else base64.b64encode(b"audio-chegando" * 100).decode(),
                        "info": {"state": "recording", "identityValid": not preload, "mimeType": "video/x-matroska;codecs=avc1,opus"},
                        "video": {"frames": 10, "width": 160, "height": 120}}
            if final_only and '__slRec.drain()' in script:
                self.assertTrue(rec.progress()['recording'],'o primeiro bloco ainda precisa entrar na cota')
                return base64.b64encode(b'audio-chegando'*100).decode()
            if "info()" in script:
                return {"width": 320, "height": 240, "mimeType": "video/x-matroska;codecs=avc1,opus"}
            return ""

        page.evaluate.side_effect = evaluate
        def wait(script,**kwargs):
            value=evaluate(script,kwargs.get('arg'))
            return SimpleNamespace(json_value=lambda:{'value':value},dispose=Mock())
        page.wait_for_function.side_effect=wait
        def open_target(*args, **kwargs):
            route = Mock()
            route.request.post_data_json = {"livestream_id": "123456"}
            route.fetch.return_value = SimpleNamespace(status=200, json=lambda: payload("123456"))
            page.guard(route)
            route.fulfill.assert_called_once()
        page.goto.side_effect = open_target
        ctx = SimpleNamespace(new_page=lambda: page)
        def tick(seconds):
            clock[0] += seconds
        with patch.object(rec, "_wait_video", return_value=True), \
                patch("recorder.time.time", side_effect=lambda: clock[0]), \
                patch('recorder.time.monotonic',side_effect=lambda:clock[0]), \
                patch("recorder.STOP.wait", side_effect=tick):
            segment, reason = rec._record_segment(ctx, 1)
        self.assertTrue(segment.exists())
        self.assertGreater(segment.stat().st_size, 0)
        self.assertTrue(rec.normalize_video)
        self.assertEqual(rec.video_size, (320, 240))
        if switch or preload:
            aborted.assert_called_once()
        return rec, reason

    def test_primeiro_bloco_no_stop_conta_tempo_sem_incluir_finalizacao_futura(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec,reason=self.record(Path(tmp),final_only=True)
            self.assertEqual(reason,'rotate')
            self.assertFalse(rec.progress()['recording'])
            self.assertEqual(rec.progress()['recorded_seconds'],1)

    def test_audio_nao_esconde_video_parado(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, reason = self.record(Path(tmp))
            self.assertEqual(reason, "stalled")

    def test_proxima_sala_e_bloqueada(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec, reason = self.record(Path(tmp), switch=True)
            self.assertEqual(reason, "finished")
            self.assertEqual(rec.live.livestream_id, "123456")

    def test_preload_de_outra_sala_nao_e_consultado(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec, reason = self.record(Path(tmp), preload=True)
            self.assertEqual(reason, "finished")
            self.assertFalse(rec._live_ended)
            self.assertEqual(rec.live.livestream_id, "123456")

    def test_live_finalizada_nao_reconecta(self):
        rec = StreamRecorder(Live("", "123456", "", 0, ""), Path("."))
        with patch("recorder.sync_playwright") as pw, \
                patch("recorder.open_context", return_value=(Mock(), Mock())), \
                patch.object(rec, "_record_segment", return_value=(None, "finished")) as record:
            rec.run()
        record.assert_called_once()

    def test_navegacao_automatica_e_bloqueada_antes_de_invalidar_o_player(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec,reason=self.record(Path(tmp),navigate=True)
            self.assertEqual(reason,'finished')
            self.assertTrue(rec._live_ended)


if __name__ == "__main__":
    unittest.main()
