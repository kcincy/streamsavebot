"""Caminhos e temporarios locais, compartilhados pelos pontos de entrada."""
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
CACHE = ROOT / "cache"
DATABASE = ROOT / "database"


def configure_runtime() -> None:
    temporary = CACHE / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(TOOLS / "browsers")
    os.environ["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
    os.environ["TMPDIR"] = os.environ["TEMP"] = os.environ["TMP"] = str(temporary)
    tempfile.tempdir = str(temporary)
    binary_dir = str(TOOLS / "ffmpeg" / "bin")
    paths = os.environ.get("PATH", "").split(os.pathsep)
    if binary_dir not in paths:
        os.environ["PATH"] = os.pathsep.join([binary_dir, *paths])


def media_tool(name: str) -> str | None:
    binary = TOOLS / "ffmpeg" / "bin" / (name + (".exe" if os.name == "nt" else ""))
    return str(binary) if binary.is_file() else None
