"""Logs limitados e sem credenciais, inclusive para erros de bibliotecas."""
import logging
from logging.handlers import RotatingFileHandler
import re

from project_runtime import ROOT


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.secrets = [variant for s in secrets if s for variant in (s,s.replace(':','~'))]

    def format(self, record):
        text = super().format(record)
        text = re.sub(r"\d{5,}[:~][A-Za-z0-9_-]{10,}", "[TOKEN]", text)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text


def configure_logging(secrets=()):
    folder = ROOT / "logs"
    folder.mkdir(exist_ok=True)
    handler = RotatingFileHandler(folder / "bot.log", maxBytes=5 * 1024 * 1024,
                                  backupCount=5, encoding="utf-8")
    handler.setFormatter(SafeFormatter(secrets))
    root = logging.getLogger()
    for old in root.handlers[:]:
        root.removeHandler(old)
        old.close()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for name in ("httpx", "httpcore", "telegram", "asyncio"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    logging.captureWarnings(True)
