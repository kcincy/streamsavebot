# Operação

## Dados e atualizações

Em instalações locais, `data/database` guarda cadastros, estados e backups;
`data/cache` guarda os vídeos temporários e `data/logs` guarda os registros.
As ferramentas ficam em `tools` e o ambiente Python em `.venv`.

No Docker, os volumes `streamsavebot_database`, `streamsavebot_cache` e
`streamsavebot_logs` mantêm os caminhos internos `/app/database`, `/app/cache` e
`/app/logs`. Isso preserva as referências dos arquivos pendentes e da Bot API.
O Dockerfile configura `STREAMSAVEBOT_DATA_DIR=/app` para esse fim.
O diretório `data/database/backups` do computador é montado no volume do banco.

Após editar apenas Python ou configurações, execute `docker compose restart bot`.
Após editar o Compose, execute `docker compose up -d`. Para dependências e arquivos
copiados para a imagem, use `docker compose up -d --build`.
Não use `docker compose down -v`: isso apaga os volumes com os dados do bot.

## Migrar uma instalação local para Docker

Pare a instância local e aguarde o encerramento completo. As credenciais ficam na
raiz do projeto. Para importar o banco e a fila uma única vez:

```powershell
New-Item -ItemType Directory -Force data/database/backups | Out-Null
docker compose build
docker compose run --rm --no-deps --entrypoint python --volume "${PWD}:/import:ro" bot -B scripts/docker_import.py /import
docker compose up -d
```

O importador aceita dados em `data/database` e `data/cache`, além do layout antigo
com essas pastas na raiz. Não sobrescreve destinos já preenchidos. Sessões de sites
configuradas precisam estar em um caminho relativo dentro do projeto.

Depois da migração, os volumes Docker são a fonte atual dos dados. Uma cópia local
antiga não deve substituir o banco do container.

## Configuração

| Arquivo/chave | Uso |
| --- | --- |
| `.env` | Token do bot e credenciais da Bot API |
| `WATERMARK_ENABLED` no `.env` | `true` ou `false` para a marca-d'água |
| `SUPERUSER_ID` | Conta isenta do limite de perfis |
| `PART_MAX_MB` | Tamanho máximo de cada arquivo; atualmente 300 MB |
| `SPOOL_MAX_MB` | Orçamento de temporários; atualmente 16384 MiB |
| `MAX_ACTIVE_CAPTURES` | Teto de capturas; CPU, memória, disco e fila podem limitar antes |
| `TEMP_RETENTION_SECONDS` | Prazo para alertar sobre uma fila atrasada; não apaga os pendentes |
| `WATERMARK_TEXT` | Texto central nos primeiros 30 segundos de cada arquivo |

O Compose define os limites de CPU e memória. Na configuração atual, 300 MB por
parte e 16 GiB de temporários permitem 52 reservas, compartilhadas entre capturas
e vídeos aguardando processamento ou envio. Não é uma garantia de 100 lives.
O código não reduz resolução ou FPS durante o processamento. No TikTok, escolhe
a melhor qualidade anunciada, usando uma alternativa quando o original não está disponível.

O modo normal aguarda recursos quando CPU ou RAM chegam a 80%. Após sobrecarga
sustentada, retoma com CPU abaixo de 65% e RAM abaixo de 70% por 30 segundos.
RAM crítica por 5 segundos ou CPU crítica por 60 segundos pode finalizar uma
captura recente e colocá-la de volta na fila. O modo `DISK_ONLY_RESOURCES`
é destinado ao ambiente Termux; veja o guia próprio antes de usá-lo.

## TikTok e estados de conexão

O TikTok usa consultas HTTP e Streamlink para abrir a melhor qualidade disponível. Um erro de
rede não confirma que o perfil está offline. Respostas incompletas são verificadas
pela página pública da live e pelos dados da sala, mantendo a identidade do perfil.

“Ao vivo · Aguardando um fluxo de vídeo” significa que a live existe, mas nenhum
fluxo de vídeo foi disponibilizado. O bot prefere o original e aceita a melhor
alternativa anunciada. Salas privadas, pagas ou restritas não são gravadas por esse caminho.

“Aguardando espaço na fila de envio” significa que as reservas de temporários
estão ocupadas. Confira `/stats` e os logs. Erros de processamento preservam o
arquivo original; um vídeo que já esteja marcado como enviado não deve ser refeito.

## Backups e envios sem confirmação

O bot mantém backups locais verificados do banco e dos arquivos pendentes.
As três cópias mais recentes são preservadas em `data/database/backups`.
Credenciais e sessões não são incluídas no ZIP. Os backups ficam no mesmo
computador; uma falha no disco também pode afetá-los.

Para verificar um ZIP pelo container:

```powershell
docker compose exec bot python -B src/backups.py /app/database/backups/NOME.zip
```

Para extrair, acrescente `--restore-to /app/cache/recuperacao-nova`. O destino deve
estar vazio. Pare o bot antes de substituir um banco e preserve o volume atual.

Um timeout depois do início de um upload pode significar que o Telegram recebeu
o vídeo sem devolver a confirmação. Esses envios são marcados como incertos e
não são repetidos automaticamente, para evitar duplicações.

## Windows sem Docker e testes

Use Python 3.10 ou superior e `scripts/setup.ps1` para criar `.venv` e instalar o
Chromium do projeto. Disponibilize FFmpeg/FFprobe em `tools/ffmpeg/bin` e o servidor
oficial em `tools/telegram-bot-api/bin`. Depois, preencha `.env`, copie
`settings.env.example` para `settings.env` e execute `python main.py`.

Os testes usam fontes locais e respostas simuladas, sem enviar mensagens reais:

```powershell
scripts/test.ps1
docker compose run --rm --no-deps --entrypoint python bot -B -m unittest discover -s tests -p "test_*.py"
```

As licenças dos componentes vendorizados ficam em `src/vendor`. O Docker instala
FFmpeg/FFprobe e compila o servidor oficial da Bot API nas versões fixadas no
Dockerfile. O Chromium é compartilhado, com contextos separados, para o SuperLive;
StripChat usa HTTP, e Kick pode abrir o navegador brevemente para obter acesso.
