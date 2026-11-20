"""Nomes, concorrencia e ordem de encerramento, sem rede."""
import concurrent.futures
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import recorder as m


class StorageShutdownTests(unittest.TestCase):
    def setUp(self):
        m.STOP.clear()
        self.addCleanup(m.STOP.clear)

    def test_reservas_concorrentes_preservam_arquivo_existente(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "alvo" / "15.11.2026"
            folder.mkdir(parents=True)
            old = folder / "alvo_09.07.mp4"
            old.write_bytes(b"original")
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                reservations = list(pool.map(lambda _: m.reserve_recording_base(
                    root, "alvo", datetime(2026, 11, 15, 9, 7)), range(4)))
            self.assertEqual({base.name for base, _ in reservations},
                             {f"alvo_09.07_{i}" for i in range(2, 6)})
            self.assertTrue(all(lock.exists() for _, lock in reservations))
            self.assertEqual(old.read_bytes(), b"original")

    def test_data_so_e_definida_quando_a_captura_comeca(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = m.StreamRecorder(m.Live("alvo", "", "", 0, ""), Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])
            with patch("recorder.datetime") as clock:
                clock.now.side_effect = [datetime(2026, 11, 15, 23, 59), datetime(2026, 11, 16, 0, 2)]
                first = rec._recording_base()
                self.assertEqual(rec._recording_base(), first)
                self.assertEqual(first.relative_to(tmp).as_posix(), "alvo/15.11.2026/alvo_23.59")
                self.assertTrue(rec._finish_session())
                second = rec._recording_base()
                self.assertEqual(second.relative_to(tmp).as_posix(), "alvo/16.11.2026/alvo_00.02")
                rec._finish_session()
            self.assertFalse(list(Path(tmp).rglob("*.lock")))

    def test_id_obtem_username_confirmado_para_o_caminho(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = m.StreamRecorder(m.Live("", "123456", "", 0, ""), Path(tmp))
            self.assertTrue(rec._accept_live({
                "stream_details": {"livestream_id": "123456", "channel_id": "canal"},
                "user": {"user_id": "111", "username": "streamer"}}))
            self.assertEqual(rec._recording_base().parent.parent.name, "streamer")
            rec._finish_session()

    def test_ctrl_c_finaliza_antes_de_fechar_e_ignora_alvo_ja_fechado(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rec = m.StreamRecorder(m.Live("alvo", "123456", "", 0, ""), out)
            events = []
            ctx, browser = Mock(), Mock()
            part = out / "part.mkv"

            def record(*args):
                part.write_bytes(b"media" * 1000)
                m.STOP.set()
                return part, "stopped"

            def finalize():
                events.append("ffmpeg")
                self.assertFalse(ctx.close.called)
                self.assertFalse(browser.close.called)
                final = out / "saved.mp4"
                final.write_bytes(part.read_bytes())
                return final

            def close_context():
                events.append("context")
                raise m.PWError("Target page, context or browser has been closed\n" + "browser log\n" * 100)

            ctx.close.side_effect = close_context
            browser.close.side_effect = lambda: events.append("browser")
            with patch("recorder.sync_playwright"), \
                    patch("recorder.open_context", return_value=(browser, ctx)), \
                    patch.object(rec, "_record_segment", side_effect=record), \
                    patch.object(rec, "_finalize", side_effect=finalize):
                result = rec.run()
            self.assertEqual(events, ["ffmpeg", "context", "browser"])
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.error, "")
            self.assertEqual(result.path.read_bytes(), part.read_bytes())

    def test_ctrl_c_nao_esconde_falha_real_na_finalizacao(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rec = m.StreamRecorder(m.Live("alvo", "123456", "", 0, ""), out)
            part = out / "original.mkv"
            part.write_bytes(b"original" * 1000)
            def record(*args):
                m.STOP.set()
                return part, "stopped"
            with patch("recorder.sync_playwright"), \
                    patch("recorder.open_context", return_value=(Mock(), Mock())), \
                    patch.object(rec, "_record_segment", side_effect=record), \
                    patch.object(rec, "_finalize", return_value=None):
                result = rec.run()
            self.assertFalse(result.ok)
            self.assertIn("preservados", result.error)
            self.assertEqual(part.read_bytes(), b"original" * 1000)

    def test_fechamento_inesperado_continua_sendo_erro_sem_despejar_logs(self):
        rec = m.StreamRecorder(m.Live("alvo", "123456", "", 0, ""), Path("."))
        with patch("recorder.sync_playwright"), \
                patch("recorder.open_context", return_value=(Mock(), Mock())), \
                patch.object(rec, "_record_segment", side_effect=m.PWError(
                    "Target page, context or browser has been closed\n" + "browser log\n" * 100)):
            result = rec.run()
        self.assertFalse(result.ok)
        self.assertNotIn("\n", result.error)
        self.assertNotIn("browser log", result.error)


if __name__ == "__main__":
    unittest.main()
