"""Configuracao do bot; nenhum segredo faz parte do repr."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import dotenv_values

from project_runtime import ROOT, CACHE, DATABASE


def number(values, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(values.get(name, default))
    except (ValueError, TypeError):
        raise ValueError(f"{name} deve ser inteiro.") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} deve estar entre {minimum} e {maximum}.")
    return value


def boolean(values,name,default=True):
    value=str(values.get(name,str(default))).strip().casefold()
    if value not in ('true','false'):raise ValueError(f'{name} deve ser true ou false.')
    return value=='true'


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    api_mode: str = "local"
    api_url: str = "http://127.0.0.1:8081"
    state: str | None = None
    segment_seconds: int = 0
    part_bytes: int = 500_000_000
    spool_bytes: int = 4096 * 1024 * 1024
    retention_seconds: int = 3600
    max_monitors_per_user: int = 3
    database: Path = DATABASE / "bot.sqlite3"
    spool: Path = CACHE / "spool"
    api_files: Path = CACHE / "telegram-bot-api" / "files"
    stripchat_state: str | None = None
    superuser_id: int | None = None
    low_resource_mode: bool = True
    max_captures: int = 100
    watermark_text: str = ''
    watermark_enabled: bool = True
    disk_only_resources: bool = False

    @property
    def watermark_active(self):return self.watermark_enabled and bool(self.watermark_text)

    @property
    def reservation_bytes(self) -> int:
        # Uma parte e tres margens globais: processamento, corte e cache da API.
        return 4 * (self.part_bytes + 8 * 1024 * 1024)

    @property
    def spool_slots(self):
        return max(1, self.spool_bytes // (self.part_bytes + 8 * 1024 * 1024) - 3)

    @classmethod
    def from_env(cls):
        values = {**dotenv_values(ROOT/'settings.env'), **os.environ}
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Defina TELEGRAM_BOT_TOKEN no .env.")
        mode = (values.get("TELEGRAM_API_MODE") or "local").strip().lower()
        if mode not in {"local", "public"}:
            raise ValueError("TELEGRAM_API_MODE deve ser local ou public.")
        url = (values.get("TELEGRAM_API_URL") or "http://127.0.0.1:8081").strip().rstrip("/")
        try:
            parsed = urlsplit(url)
            port = parsed.port
            if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}
                    or not port or parsed.username or parsed.password or parsed.path
                    or parsed.query or parsed.fragment):
                raise ValueError()
        except ValueError:
            raise ValueError("TELEGRAM_API_URL deve ser http://127.0.0.1:PORTA.") from None
        states={}
        for name in ('SUPERLIVE_STATE','STRIPCHAT_STATE'):
            state = (values.get(name) or '').strip()
            if state:
                path = Path(state).expanduser()
                path = path if path.is_absolute() else ROOT / path
                if not path.is_file():
                    raise ValueError(f'{name} aponta para uma sessao inexistente.')
                state = str(path.resolve())
            states[name]=state or None
        part_mb=number(values,'PART_MAX_MB',500 if mode=='local' else 45,32,500 if mode=='local' else 45)
        watermark=(values.get('WATERMARK_TEXT') or '').strip()
        if len(watermark)>200 or any(ord(c)<32 for c in watermark):
            raise ValueError('WATERMARK_TEXT deve ter uma linha com ate 200 caracteres.')
        config = cls(token, mode, url, states['SUPERLIVE_STATE'],
                     0, part_mb*1_000_000,
                     number(values, "SPOOL_MAX_MB", 4096, 96, 16384) * 1024 * 1024,
                     number(values, "TEMP_RETENTION_SECONDS", 3600, 60, 86400),
                     number(values, "MAX_MONITORS_PER_USER", 3, 1, 3),
                     stripchat_state=states['STRIPCHAT_STATE'],
                     superuser_id=number(values,'SUPERUSER_ID',0,0,2**63-1) or None,
                     low_resource_mode=bool(number(values,'LOW_RESOURCE_MODE',1,0,1)),
                     max_captures=number(values,'MAX_ACTIVE_CAPTURES',100,1,100),
                     watermark_text=watermark,watermark_enabled=boolean(values,'WATERMARK_ENABLED'),
                     disk_only_resources=bool(number(values,'DISK_ONLY_RESOURCES',0,0,1)))
        if config.spool_bytes < config.reservation_bytes:
            raise ValueError("SPOOL_MAX_MB precisa comportar quatro partes de PART_MAX_MB com margem.")
        return config
