# StreamSaveBot

Bot do Telegram para monitorar perfis e receber gravações do SuperLive, StripChat,
Kick e TikTok. Interface em português e inglês, gravações por partes, folhas de
contato 3×3 e controle de recursos para organizar a fila de captura e envio.

## Instalação nova com Docker

Use Docker Desktop com contêineres Linux. No PowerShell, dentro da pasta do projeto:

```powershell
Copy-Item .env.telegram.example .env
Copy-Item settings.env.example settings.env
```

Preencha `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID` e `TELEGRAM_API_HASH` no `.env`.
Configure `SUPERUSER_ID` em `settings.env` com seu ID do Telegram, caso queira uma
conta administradora. O valor `0` deixa essa função sem usuário configurado.

Construa a imagem e inicialize o banco vazio para esta instalação nova:

```powershell
docker compose build
docker compose run --rm --no-deps --entrypoint python bot -B -c "import sys; from pathlib import Path; sys.path.insert(0, 'src'); from storage import Store; from contact_sheets import initialize; Store(Path('database/bot.sqlite3')).close(); initialize(Path('database/sheets'))"
docker compose up -d
```

Depois, abra uma conversa privada com seu bot e envie `/start`.

**Se você já tem dados do bot**, siga a [migração com Docker](docs/DOCKER.md).
Preserve seus bancos e volumes existentes; não repita a importação nas atualizações.

## Operação

```powershell
docker compose logs -f --tail 50 bot
docker compose exec bot tail -n 80 -f /app/logs/bot.log
docker stats --no-stream
```

Após alterações somente no código Python ou nas configurações:

```powershell
docker compose restart bot
```

Alterações nas dependências ou no Dockerfile exigem reconstruir a imagem. Um
reinício finaliza as partes atuais e pode deixar uma lacuna até retomar as capturas.

## Documentação

- [Comandos, funcionamento e estrutura](docs/README.md)
- [Docker, migração e volumes](docs/DOCKER.md)
- [Ambiente Windows](docs/AMBIENTE_LOCAL.md)
- [Plataformas](docs/PLATAFORMAS.md)
- [Recursos e desempenho](docs/PERFORMANCE.md)
- [Operação, recuperação e backups](docs/OPERACAO.md)
- [Marca-d'água](docs/WATERMARK.md)

O repositório contém código, testes e configurações de exemplo. Credenciais,
sessões, bancos, gravações, imagens geradas, logs e binários locais ficam fora do Git.
O Dockerfile instala as ferramentas necessárias para o contêiner.
