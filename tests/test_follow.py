"""Acompanhamento de conta, retomadas e separacao de transmissoes, sem rede."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recorder import Live, StreamRecorder


def profile(lid, uid="111", username="alvo"):
    return {"user": {"user_id": uid, "username": username, "livestream_id": lid}}


class FollowTests(unittest.TestCase):
    def test_cached_profile_lookup_uses_http_and_still_rejects_reassigned_account(self):
        rec=StreamRecorder(Live('alvo','','',0,'',user_id='111'),Path('.'))
        rec._profile_request={'url_or_request':'https://example.invalid/profile','method':'POST','data':'{}'}
        response=Mock(status=200);response.json.return_value=profile('123456')
        context=Mock();context.request.fetch.return_value=response
        self.assertEqual(rec._resolve_profile(context).livestream_id,'123456')
        context.new_page.assert_not_called();response.dispose.assert_called_once()
        response.json.return_value=profile('234567',uid='222')
        with self.assertRaisesRegex(ValueError,'outra conta'):rec._resolve_profile(context)
        context.new_page.assert_not_called()

    def test_username_acompanha_mas_id_permanece_restrito(self):
        self.assertEqual(StreamRecorder(Live("alvo", "", "", 0, ""), Path(".")).follow_username, "alvo")
        self.assertFalse(StreamRecorder(Live("alvo", "123456", "", 0, ""), Path(".")).follow_username)
        self.assertTrue(Live("alvo", "123456", "", 0, "").url.endswith("/livestream/123456"))

    def test_perfil_offline_fixa_conta_e_aceita_novo_id_da_mesma_conta(self):
        rec = StreamRecorder(Live("alvo", "", "", 0, ""), Path("."))
        self.assertIsNone(rec._profile_live(profile(None)))
        self.assertEqual(rec.target_user_id, "111")
        self.assertEqual(rec._profile_live(profile("123456")).livestream_id, "123456")
        self.assertEqual(rec._profile_live(profile("234567")).livestream_id, "234567")
        self.assertEqual(rec.live.livestream_id, "")

    def test_urls_de_perfil_sempre_identificam_a_conta(self):
        for url in ("https://superlivechat.tv/pt/alvo", "https://superlivechat.tv/en/alvo",
                    "https://superlivechat.tv/alvo"):
            with self.subTest(url=url):
                rec = StreamRecorder(Live("", "", "", 0, "", url_override=url), Path("."))
                self.assertEqual(rec.follow_username, "alvo")
        with self.assertRaises(ValueError):
            StreamRecorder(Live("", "", "", 0, "", url_override="https://superlivechat.tv/"), Path("."))

    def test_username_reutilizado_por_outra_conta_e_rejeitado(self):
        rec = StreamRecorder(Live("alvo", "", "", 0, ""), Path("."))
        rec._profile_live(profile("123456"))
        with self.assertRaisesRegex(ValueError, "outra conta"):
            rec._profile_live(profile("234567", uid="222"))
        self.assertEqual(rec.target_user_id, "111")

    def test_perfil_diferente_nao_altera_identidade(self):
        rec = StreamRecorder(Live("alvo", "", "", 0, ""), Path("."))
        with self.assertRaises(ValueError):
            rec._profile_live(profile("123456", username="outro"))
        self.assertEqual(rec.target_user_id, "")

    def test_sala_com_mesmo_id_e_username_mas_outra_conta_e_bloqueada(self):
        rec = StreamRecorder(Live("alvo", "123456", "", 0, "", user_id="111"), Path("."))
        data = profile("123456", uid="222")
        data["stream_details"] = {"livestream_id": "123456", "channel_id": "x"}
        self.assertFalse(rec._accept_live(data))
        self.assertEqual(rec.target_user_id, "111")
        self.assertTrue(rec._identity_error)

    def test_tres_reinicios_geram_quatro_arquivos_sem_misturar_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rec = StreamRecorder(Live("alvo", "", "", 0, ""), out)
            ids = ["123456", "234567", "345678", "456789"]
            responses = [value for lid in ids for value in (profile(lid), profile(None))]
            responses.append(profile("999999", uid="222"))
            iterator = iter(responses)

            def resolve(ctx):
                return rec._profile_live(next(iterator))

            def record(ctx, index):
                path = out / f"{rec.live.livestream_id}.part{index}.mkv"
                path.write_bytes(rec.live.livestream_id.encode() * 1000)
                return path, "finished"

            def finalize():
                # Uma transmissao nao pode receber segmentos de outra.
                for part in rec.segments:
                    self.assertTrue(part.name.startswith(rec.live.livestream_id + "."))
                path = out / f"{rec.live.livestream_id}.mkv"
                path.write_bytes(b"".join(p.read_bytes() for p in rec.segments))
                return path

            with patch("recorder.sync_playwright"), \
                    patch("recorder.open_context", return_value=(Mock(), Mock())), \
                    patch("recorder.STOP.wait", return_value=False), \
                    patch.object(rec, "_resolve_profile", side_effect=resolve), \
                    patch.object(rec, "_record_segment", side_effect=record) as records, \
                    patch.object(rec, "_finalize", side_effect=finalize):
                result = rec.run()
            self.assertEqual(records.call_count, 4)
            self.assertEqual([p.stem for p in result.paths], ids)
            self.assertEqual(rec.target_user_id, "111")
            self.assertIn("outra conta", result.error)
            self.assertFalse(result.ok)
            self.assertTrue(all(p.exists() for p in result.paths))

    def test_no_reconnect_nao_aguarda_perfil_offline(self):
        rec = StreamRecorder(Live("alvo", "", "", 0, ""), Path("."), reconnect=0)
        with patch("recorder.sync_playwright"), \
                patch("recorder.open_context", return_value=(Mock(), Mock())), \
                patch.object(rec, "_resolve_profile", return_value=None) as resolve, \
                patch.object(rec, "_record_segment") as record:
            result = rec.run()
        self.assertFalse(result.ok)
        resolve.assert_called_once()
        record.assert_not_called()

    def test_duracao_limita_espera_offline(self):
        rec = StreamRecorder(Live("alvo", "", "", 0, ""), Path("."), duration=3)
        clock = [1000.0]
        with patch("recorder.sync_playwright"), \
                patch("recorder.open_context", return_value=(Mock(), Mock())), \
                patch.object(rec, "_resolve_profile", return_value=None), \
                patch("recorder.time.time", side_effect=lambda: clock[0]), \
                patch("recorder.STOP.wait", side_effect=lambda sec: clock.__setitem__(0, clock[0] + sec)):
            result = rec.run()
        self.assertEqual(result.seconds, 3)
        self.assertFalse(result.ok)

    def test_consulta_lenta_nao_inicia_captura_depois_do_limite(self):
        rec = StreamRecorder(Live("alvo", "", "", 0, ""), Path("."), duration=3)
        clock = [1000.0]
        def resolve(ctx):
            clock[0] += 4
            return rec._profile_live(profile("123456"))
        with patch("recorder.sync_playwright"), \
                patch("recorder.open_context", return_value=(Mock(), Mock())), \
                patch.object(rec, "_resolve_profile", side_effect=resolve), \
                patch.object(rec, "_record_segment") as record, \
                patch("recorder.time.time", side_effect=lambda: clock[0]):
            rec.run()
        record.assert_not_called()

    def test_falha_na_finalizacao_preserva_transmissao_e_nao_segue_adiante(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = StreamRecorder(Live("alvo", "", "", 0, ""), Path(tmp))
            part = Path(tmp) / "original.part01.mkv"
            part.write_bytes(b"original" * 1000)
            with patch("recorder.sync_playwright"), \
                    patch("recorder.open_context", return_value=(Mock(), Mock())), \
                    patch.object(rec, "_resolve_profile", return_value=rec._profile_live(profile("123456"))), \
                    patch.object(rec, "_record_segment", return_value=(part, "finished")) as record, \
                    patch.object(rec, "_finalize", return_value=None) as finalize:
                result = rec.run()
            self.assertFalse(result.ok)
            self.assertIn("preservados", result.error)
            self.assertEqual(part.read_bytes(), b"original" * 1000)
            record.assert_called_once()
            finalize.assert_called_once()

    def test_rotacao_nao_sobrescreve_segmentos_nem_gasta_reconexoes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rec = StreamRecorder(Live("alvo", "123456", "", 0, ""), out,
                                 reconnect=0, rotate=1)
            calls = []
            def record(ctx, index):
                calls.append(index)
                part = out / f"part{index}.mkv"
                part.write_bytes(str(index).encode() * 3000)
                return part, "rotate" if len(calls) < 3 else "stopped"
            def finalize():
                self.assertEqual(len(set(rec.segments)), 3)
                self.assertEqual([p.read_bytes()[:1] for p in rec.segments], [b"1", b"2", b"3"])
                final = out / "final.mkv"
                final.write_bytes(b"final")
                return final
            with patch("recorder.sync_playwright"), \
                    patch("recorder.open_context", return_value=(Mock(), Mock())), \
                    patch.object(rec, "_record_segment", side_effect=record), \
                    patch.object(rec, "_finalize", side_effect=finalize):
                result = rec.run()
            self.assertTrue(result.ok)
            self.assertEqual(calls, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
