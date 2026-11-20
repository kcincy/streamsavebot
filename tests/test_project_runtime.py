"""Ferramentas e temporarios locais; nenhum fallback para FFmpeg global."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import project_runtime as runtime
import recorder as engine


class ProjectRuntimeTests(unittest.TestCase):
    def test_dependencias_e_binarios_locais(self):
        import playwright, telegram, dotenv
        project = runtime.ROOT
        for module in (playwright, telegram, dotenv):
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(project / '.venv'))
        runtime.configure_runtime()
        self.assertEqual(Path(os.environ['PLAYWRIGHT_BROWSERS_PATH']), project / 'tools' / 'browsers')
        self.assertEqual(Path(tempfile.gettempdir()), project / 'cache' / 'tmp')
        self.assertEqual(Path(os.environ['TMPDIR']), Path(tempfile.gettempdir()))
        self.assertEqual(Path(runtime.media_tool('ffmpeg')), project / 'tools' / 'ffmpeg' / 'bin' / ('ffmpeg.exe' if os.name=='nt' else 'ffmpeg'))
        rec = engine.StreamRecorder(engine.Live('alvo', '', '', 0, ''), project / 'recordings')
        self.assertEqual(rec.ffmpeg, runtime.media_tool('ffmpeg'))

    def test_ferramenta_ausente_nao_usa_instalacao_global(self):
        with patch.object(runtime, 'TOOLS', runtime.ROOT / 'cache' / 'inexistent-tools'):
            self.assertIsNone(runtime.media_tool('ffmpeg'))

    def test_ffmpeg_explicito_continua_funcionando(self):
        rec = engine.StreamRecorder(engine.Live('alvo', '', '', 0, ''), runtime.ROOT / 'recordings',
                                    ffmpeg='custom-ffmpeg.exe')
        self.assertEqual(rec.ffmpeg, 'custom-ffmpeg.exe')

