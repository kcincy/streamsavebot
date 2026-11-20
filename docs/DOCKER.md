# Docker com Linux

O projeto usa um contêiner Ubuntu 24.04 com Python em /app/.venv, FFmpeg/FFprobe 9.0 do Linux (build fixado e verificado por SHA-256),
Chromium da mesma versão do Playwright de requirements.txt e Bot API oficial compilada do
commit 7232881b39dfcd048a0d569f1983c5184249ee05 (10.4). Não executa binários Windows nem Wine.
O SuperLive usa um viewer mínimo com o SDK Web oficial do Agora, um único canal RTC
e qualidade HIGH sem fallback para resolução inferior. O Chromium compartilhado ainda
é necessário para o WebRTC. A página completa serve apenas para obter a sessão normal
do site e é fechada antes da captura. Salas com convidados também usam somente o publisher
principal do canal confirmado. Se o viewer mínimo falhar, o player original é limitado
a esse canal quando a conexão do SDK puder ser correlacionada. Um SDK ainda não reconhecido
mantém a captura original para evitar perda de gravação; esse caso continua mais pesado e é
identificado no log. As verificações de identidade permanecem. O StripChat usa HTTP, sem navegador.
Kick e TikTok usam Streamlink e FFmpeg por copia, sem reduzir a resolucao.
O FFmpeg 6.1 do Ubuntu nao reconhece os tags HEVC legados usados por algumas lives
do TikTok. Por isso o Dockerfile instala um build 9.0 fixado por URL e checksum, com
FFmpeg e FFprobe da mesma versao em /usr/local/bin. A consulta e a captura permanecem
via Streamlink; essa compatibilidade nao usa uma qualidade inferior. A Kick pode
abrir o Chromium brevemente para obter cookies de acesso, sem usar o player para gravar.

Esta atualizacao adiciona Streamlink as dependencias: execute `docker compose up -d --build`.
Mudancas posteriores somente no codigo Python continuam exigindo apenas `docker compose restart bot`.
O idioma, as inscrições e a fila ficam persistidos no volume do banco; não reimporte dados.
Não há limite semanal de horas. O cadastro comum permanece limitado a três perfis.

O Docker Desktop precisa estar aberto, usando contêineres Linux/WSL 2. Comece com pelo menos
8 GB de RAM disponíveis para a VM do Docker; o contêiner tem limite de 6 CPUs e 6 GB de RAM.
Esses são limites iniciais para proteger o PC, não uma garantia de quantidade de lives simultâneas.
Altere cpus/mem_limit em compose.yaml se a medição com seus perfis mostrar necessidade.

`PART_MAX_MB=300` define partes de até 300 MB, sem mudar resolução, FPS ou codec de origem.
`SPOOL_MAX_MB=16384` reserva até 16 GiB para temporários e permite 52 partes simultâneas,
somando gravações e fila. O limite padrão, se PART_MAX_MB estiver ausente, permanece 500 MB
na API local e 45 MB na API pública; a API pública exige PART_MAX_MB no máximo 45.
Os 16 GiB são um teto de uso, não memória RAM reservada. Se a fila crescer, ela também
consome essas vagas; o buffer limitado não substitui upload suficiente para as fontes.
Veja [medições e limites de capacidade](PERFORMANCE.md).

## Primeira migração

1. Pare o bot Windows com Ctrl+C e aguarde `encerramento concluido` em logs/bot.log.
   Não rode o bot Windows e o Docker ao mesmo tempo: ambos usam o mesmo token.
2. Mantenha .env e settings.env na raiz. As credenciais são montadas somente para leitura;
   não entram no contexto de build nem na imagem. Se SUPERLIVE_STATE/STRIPCHAT_STATE estiver
   preenchido, use um caminho relativo para um JSON dentro do projeto, como sessions/superlive.json.
3. No PowerShell, na pasta do projeto, execute:

```powershell
New-Item -ItemType Directory -Force database/sheets/images | Out-Null
docker compose build
docker compose run --rm --no-deps --entrypoint /app/.venv/bin/python --volume "${PWD}:/import:ro" bot -B scripts/docker_import.py /import
```

A primeira construção pode demorar: compila o servidor oficial com dois processos para limitar
o consumo de memória e instala somente o Chromium completo, sem os outros navegadores.
Construções seguintes reaproveitam as camadas enquanto as dependências não mudarem.

A importação copia banco, histórico, inscrições, fila, spool e cache/sessão da Bot API para volumes
Linux. Faz uma cópia consistente do SQLite e verifica sua integridade; converte referências
cache/spool de Windows para /app/cache/spool sem mudar IDs, destinatários ou estados.
Sessões de navegador configuradas são copiadas para database/sessions com permissão restrita.
Os arquivos de origem no Windows permanecem intactos. A importação recusa volumes já preenchidos.
Se falhar, não inicie o serviço; confira o erro e preserve tanto a origem quanto os volumes.

O iniciador recusa executar sem banco importado, evitando criar um bot vazio por engano.

## Verificar antes de iniciar

Os testes abaixo usam servidores locais e não enviam mensagens pelo Telegram:

```powershell
docker compose run --rm --no-deps --entrypoint /app/.venv/bin/python bot -B -m unittest discover -s tests -p "test_stripchat*.py"
docker compose run --rm --no-deps --entrypoint /app/.venv/bin/python bot -B -m unittest discover -s tests -p "test_browser_pool.py"
```

Eles verificam captura HTTP/MOUFLON, FFmpeg real, privacidade, /stop, duas capturas independentes
e o navegador compartilhado. Não comprovam conectividade ou estabilidade de uma live real.

## Executar e acompanhar

```powershell
docker compose up -d
docker compose logs -f --tail 50 bot
docker compose exec bot tail -n 100 -f /app/logs/bot.log
docker stats --no-stream
```

O log detalhado continua em /app/logs/bot.log, com rotação. A saída do contêiner também é limitada.
Não há portas publicadas: a Bot API e o controle do Chromium ficam no loopback do contêiner.
O serviço roda sem root, com filesystem da imagem somente para leitura, init para recolher
processos filhos e /dev/shm de 1 GB para o Chromium. O banco, o spool e os logs ficam em volumes
nomeados (streamsavebot_database, streamsavebot_cache, streamsavebot_logs), para evitar as
operações frequentes de SQLite/temporários através do filesystem Windows.

O runtime define `TMPDIR`, `TEMP` e `TMP` como `/app/cache/tmp`. O Chromium no Linux
usa `TMPDIR` também para o socket do perfil; sem essa variável ele tenta usar `/tmp`,
que não aceita escrita com a raiz somente para leitura, e os monitores ficam reconectando.

restart: unless-stopped retoma o serviço após falhas/reinício do Docker, respeitando uma parada manual.
O encerramento envia SIGTERM e dá até 35 minutos para terminar uploads em andamento, cujo timeout
pode chegar a 30 minutos. Uma interrupção forçada ainda pode deixar um envio sem confirmação;
a recuperação existente preserva arquivos e evita reenviar uploads incertos automaticamente.

```powershell
docker compose stop
```

Depois da migração, os volumes Docker são a fonte atual dos usuários, histórico e fila. A cópia Windows
não é sincronizada automaticamente e não deve ser usada para retomar um estado antigo.
`docker compose down` preserva os volumes. Não use `down -v` nem remova os volumes para atualizar.
## Atualizar o bot

O Compose monta `main.py` e `src` diretamente da pasta do projeto, somente para leitura.
Após receber esta configuração, execute uma vez `docker compose up -d` para recriar
o contêiner com as montagens. Isso também permite carregar o código atual sem reconstruir
a imagem, desde que suas dependências continuem compatíveis.

Depois de editar arquivos em `src` ou `main.py`, basta:

```powershell
docker compose restart bot
```

As alterações ficam disponíveis na pasta montada imediatamente; o reinício carrega os módulos
Python atualizados. Aplique um conjunto completo de alterações antes de reiniciar. O reinício
finaliza as gravações atuais e pode deixar uma lacuna até a retomada do monitoramento.
O prazo de encerramento continua sendo de até 35 minutos, para concluir uploads pendentes.
Os bancos e vídeos temporários continuam nos volumes Linux. As imagens das folhas de contato
são gravadas diretamente em `database/sheets/images` no Windows.

Mudanças em `.env` ou `settings.env` também precisam de reinício para serem relidas.
Mudanças no `compose.yaml` precisam de `docker compose up -d`, pois `restart` não aplica
alterações na configuração do contêiner.

Reconstrua ao alterar `requirements.txt`, `Dockerfile`, `docker/entrypoint.sh` ou outros arquivos
copiados para a imagem, como os testes e o importador. Não repita a importação:

```powershell
docker compose stop
docker compose build
docker compose up -d
```

Para salvar uma cópia em uma pasta nova docker-backup, com o serviço parado:

```powershell
docker compose stop
docker compose run --rm --no-deps --entrypoint /app/.venv/bin/python --volume "${PWD}/docker-backup:/backup" bot -c "import shutil; shutil.copytree('/app/database','/backup/database'); shutil.copytree('/app/cache','/backup/cache')"
```

Use uma pasta vazia; o comando recusa sobrescrever um backup existente. O banco sozinho não contém
os vídeos pendentes. Não sobrescreva o banco Windows com uma cópia ativa do SQLite.

Referências: [Playwright no Docker](https://playwright.dev/python/docs/docker),
[recursos do Compose](https://docs.docker.com/reference/compose-file/services/),
[servidor oficial Telegram Bot API](https://github.com/tdlib/telegram-bot-api).

## Folhas de contato das gravações

Cada nova parte capturada gera `database/sheets/images/<id-da-gravacao>.jpg` e um
registro em `database/sheets/sheets.sqlite3`, tabela `recordings`. Se a parte precisar
ser dividida em vários arquivos para envio, uma única folha cobre a duração somada.
`live_id` e `part_number` permitem agrupar partes da mesma live.

São nove imagens em ordem cronológica, da esquerda para a direita, de cima para baixo,
nos centros de nove intervalos iguais da duração do vídeo. A folha tem sempre 1440×810
pixels (16:9), com nove quadros de 480×270. Os frames recebem recorte central para
preencher cada quadro sem distorção; em vídeos verticais, parte do topo e da base é cortada.
A grade não acrescenta textos, bordas, margens,
espaços ou marcas. O FFmpeg busca cada posição separadamente, com uma thread por vez.
Clipes com menos de nove frames podem repetir imagens disponíveis.

Os horários de início e término são ISO 8601 em UTC, com `+00:00`. A duração em segundos
é a do vídeo efetivamente salvo, não a duração total da transmissão anterior ao monitoramento.
`profile_url` aponta para o perfil original na plataforma, usando sua URL canônica.
`title` fica NULL quando a plataforma não fornece título; `telegram_user_ids` é sempre
um array JSON dos participantes daquela captura, incluindo quem saiu durante a gravação.
O catálogo também preserva identidade do perfil, plataforma, quantidade de arquivos,
data de criação e estado da folha (`pending`, `ready` ou `failed`).

A folha é gerada antes de liberar o envio. Se falhar, o erro fica no catálogo e a origem
é preservada pelo prazo de recuperação já configurado. Imagens e catálogo não são apagados
pela limpeza de vídeos temporários; seu espaço em disco cresce com o histórico.
Os JPGs são gravados diretamente na pasta `database/sheets/images` do projeto no Windows,
por uma montagem de pasta com escrita. O catálogo SQLite continua no volume
`streamsavebot_database`; o banco local de mesmo nome não é sincronizado. O backup acima
inclui tanto o catálogo quanto as imagens compartilhadas.

Se o bot já gerava imagens antes desta configuração, copie o histórico uma única vez,
usando o contêiner existente, antes de executar `up` com a nova montagem:

```powershell
docker compose stop bot
New-Item -ItemType Directory -Force database/sheets/images | Out-Null
docker compose cp bot:/app/database/sheets/images/. ./database/sheets/images
docker compose up -d
```

Espere a cópia terminar sem erros antes do último comando. O contêiner deve estar parado
durante a cópia para finalizar as gravações e impedir novas imagens nesse intervalo.
Se ainda não existirem imagens no contêiner, basta criar a pasta e executar `docker compose up -d`.
`restart` sozinho não aplica a nova montagem. Depois disso, as imagens novas já aparecem no Windows.

Para copiar também o catálogo, com o serviço parado, para um destino novo:

```powershell
docker compose stop
docker compose cp bot:/app/database/sheets ./sheets-export
docker compose up -d
```

Use um destino novo. Para ativar mudanças no código, siga a seção de atualização acima,
sem repetir a importação.
