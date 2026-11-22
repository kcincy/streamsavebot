#!/bin/sh
set -eu

if [ ! -f /app/database/bot.sqlite3 ]; then
    echo 'Inicialize ou importe os dados primeiro: veja README.md e docs/OPERACAO.md.' >&2
    exit 1
fi
if [ -f /app/database/sessions/superlive.json ]; then
    export SUPERLIVE_STATE=/app/database/sessions/superlive.json
fi
if [ -f /app/database/sessions/stripchat.json ]; then
    export STRIPCHAT_STATE=/app/database/sessions/stripchat.json
fi
exec /app/.venv/bin/python -B /app/main.py
