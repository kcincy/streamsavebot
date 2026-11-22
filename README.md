# StreamSaveBot

Bot do Telegram para acompanhar e gravar lives do SuperLive, StripChat, Kick e
TikTok. Interface em português e inglês, vídeos por partes e a melhor qualidade disponível.

## Executar com Docker

Use Docker Desktop com contêineres Linux. Para uma instalação nova:

```powershell
Copy-Item .env.telegram.example .env
Copy-Item settings.env.example settings.env
New-Item -ItemType Directory -Force data/database/backups | Out-Null
```

Preencha `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID` e `TELEGRAM_API_HASH` no `.env`.
Configure seu ID do Telegram em `SUPERUSER_ID`, no `settings.env`, se quiser uma
conta administradora. O valor `0` deixa essa função sem usuário configurado.

```powershell
docker compose build
docker compose run --rm --no-deps --entrypoint python bot -B -c "import sys; from pathlib import Path; sys.path.insert(0, 'src'); from storage import Store; Store(Path('database/bot.sqlite3')).close()"
docker compose up -d
```

Abra uma conversa privada com o bot e envie `/start`.
Se já usa o bot, preserve seus volumes e siga a [operação e migração](docs/OPERACAO.md).
Não repita a importação nem execute duas instâncias com o mesmo token.

## Comandos

| Comando | Função |
| --- | --- |
| `/start` | Boas-vindas e escolha de idioma |
| `/add` | Adicionar um perfil |
| `/monitor` | Ver os perfis, pausar, retomar ou remover |
| `/stop` | Enviar a parte atual e continuar gravando |
| `/remove` | Remover um perfil |
| `/stats` | Consultar o histórico e as estatísticas |

Usuários comuns podem cadastrar até três perfis, incluindo pausados. Não há cota
semanal de gravação. Cada perfil usa uma captura compartilhada entre os inscritos.

## Estrutura

```text
src/          aplicação e dependências vendorizadas
tests/        testes com fontes locais e respostas controladas
scripts/      instalação, inicialização e importação
docs/         operação e Termux
data/         bancos, backups, temporários e logs locais
tools/        ferramentas instaladas no computador
```

Credenciais ficam no `.env`; configurações operacionais ficam no `settings.env`.
Ambos são locais. Exemplos seguros são versionados no repositório. No Docker, os
bancos, vídeos temporários e logs continuam nos volumes nomeados existentes;
os backups são gravados em `data/database/backups` no computador.

## Atualizar e acompanhar

```powershell
docker compose restart bot
docker compose logs -f --tail 50 bot
docker compose exec bot tail -n 80 -f /app/logs/bot.log
docker stats --no-stream
```

Mudanças em dependências, Dockerfile ou scripts copiados para a imagem exigem
`docker compose up -d --build`. Um reinício finaliza as partes atuais e pode deixar
uma lacuna até retomar as capturas.

Consulte [operação, recuperação e backups](docs/OPERACAO.md) e
[instalação no Android/Termux](docs/TERMUX.md).
