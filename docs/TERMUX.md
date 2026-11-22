# StreamSaveBot no Termux

Este guia é para uma instalação nova no Android sem root. O Termux roda um
Ubuntu 24.04 via PRoot para instalar Python, Playwright e Chromium para Linux.
O setup usa FFmpeg 9.0 para ler também os fluxos FLV/HEVC do TikTok. Separe alguns
GB livres para Ubuntu, Chromium e gravações. [Playwright](https://playwright.dev/python/docs/intro) · [Build do FFmpeg](https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/tag/autobuild-2026-11-19-12-39)

## 1. Preparar o Termux

Instale o Termux de uma das fontes oficiais. No Termux, rode `uname -m` e confira
se aparece `aarch64` ou `x86_64`. Os comandos abaixo são para o Termux. [Downloads do Termux](https://github.com/termux/termux-app#installation)

```bash
pkg update && pkg upgrade
pkg install proot-distro tmux
proot-distro install ubuntu:24.04 --name ubuntu
proot-distro login ubuntu
```

`proot-distro install` é necessário uma vez. Nas próximas sessões, entre com
`proot-distro login ubuntu`. [Documentação do PRoot-Distro](https://github.com/termux/proot-distro#commands-reference)

## 2. Clonar o repositório privado

Rode os próximos comandos **dentro do Ubuntu**. Entre no GitHub com uma conta
com acesso a `kcincy/streamsavebot`. Se o `gh` mostrar um código, abra no navegador
do celular a página indicada e informe esse código. [GitHub CLI](https://cli.github.com/manual/gh_auth_login)

```bash
apt-get update && apt-get install -y ca-certificates git gh nano
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git --hostname github.com
cd ~
git clone https://github.com/kcincy/streamsavebot.git
cd streamsavebot
```

Use a revisão do projeto que inclui o suporte ao Termux. Se os arquivos desta
entrega ainda estiverem só no computador, inclua as alterações no Git ou transfira
`setup-termux.sh`, `termux-settings.env.example`, `config.py` e `resource_control.py`
para `Download` no Android. Para usar `Download`, rode `termux-setup-storage` no
Termux e conceda o acesso. Depois, dentro do clone no Ubuntu, copie os arquivos:

```bash
cp /sdcard/Download/setup-termux.sh /sdcard/Download/termux-settings.env.example scripts/
cp /sdcard/Download/config.py /sdcard/Download/resource_control.py src/
```

Mantenha o clone em `~/streamsavebot`, no armazenamento interno do Ubuntu. Assim
o ambiente virtual, os executáveis e os bancos SQLite usam o sistema de arquivos
do Linux. [Guia de armazenamento do Termux](https://github.com/termux/termux-tools/blob/master/doc/termux.1.md.in#accessing-the-storage)

## 3. Instalar e configurar o bot

```bash
bash scripts/setup-termux.sh
nano .env
```

Crie um bot com `/newbot` no `@BotFather` e preencha `TELEGRAM_BOT_TOKEN` no `.env`.
Salve com `Ctrl+O`, `Enter` e `Ctrl+X`. Guarde o token como senha. Se for usar um
bot já existente, encerre a outra instância antes de iniciar esta. Para testar
no celular enquanto o bot original continua ativo, use um token novo.
[Como obter um token](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)

O instalador usa o `requirements.txt` do projeto, coloca Chromium em
`tools/browsers` e FFmpeg/FFprobe em `tools/ffmpeg/bin`. Ele cria `.env` e
`settings.env` quando esses arquivos estiverem ausentes, preservando edições ao
rodar novamente. O bot inicializa os bancos SQLite no primeiro uso.

O `settings.env` para o celular começa no modo `public`, com `PART_MAX_MB=45`,
`MAX_ACTIVE_CAPTURES=2` e um limite de 1 GiB para a fila temporária. Só o token é
necessário para a API pública. `SUPERUSER_ID` pode receber seu ID numérico do
Telegram se quiser configurar um administrador. [Limites da API do Telegram](https://core.telegram.org/bots/features#local-bot-api)

O PRoot pode não disponibilizar as métricas de CPU e memória do cgroup do
Docker. `DISK_ONLY_RESOURCES=1` ativa a checagem de espaço em disco para o Termux.
O bot continua respeitando os limites de partes, fila e capturas simultâneas.
Deixe pelo menos 1 GiB livre durante a operação para que novas capturas possam começar.
Se `settings.env` já existia, confira se ele inclui essa opção e os valores do
modelo de configuração do Termux.

## 4. Iniciar e testar

```bash
.venv/bin/python -u main.py
```

Quando aparecer `Bot iniciado`, envie `/start` em uma conversa privada com o bot.
Escolha o idioma, use `/add` para cadastrar um perfil e confira o estado com
`/monitor`. Para testar o envio, acompanhe uma live pública até a primeira parte
chegar. Encerre com `Ctrl+C` e aguarde o fechamento das capturas e da fila.

O SuperLive e algumas consultas da Kick usam Chromium. Se o bot responder, mas
essas plataformas falharem, teste o navegador no Ubuntu, dentro do clone e com o
bot parado:

```bash
.venv/bin/python - <<'PY'
import sys
sys.path.insert(0, 'src')
from project_runtime import configure_runtime
from recorder import open_context, sync_playwright

configure_runtime()
with sync_playwright() as pw:
    browser, context = open_context(pw, False)
    page = context.new_page()
    page.set_content('<title>Chromium OK</title>')
    assert page.title() == 'Chromium OK'
    browser.close()
print('Chromium OK')
PY
```

Se o Chromium fechar na abertura, atualize os pacotes no Termux com `pkg upgrade`
e entre de novo no Ubuntu. O PRoot corrigiu uma falha de inicialização do
Chromium 150+ a partir da versão 5.1.107.96. [Correção no PRoot](https://github.com/termux/proot/issues/404)

## 5. Operar e atualizar

Para manter a sessão mesmo ao fechar a tela do terminal, rode no **Termux**:

```bash
termux-wake-lock
tmux new -s streamsavebot
proot-distro login ubuntu
cd ~/streamsavebot
.venv/bin/python -u main.py
```

Desanexe com `Ctrl+B` e depois `D`. Volte com `tmux attach -t streamsavebot`.
Quando parar o bot, libere o bloqueio com `termux-wake-unlock` no Termux. O `tmux`
mantém a sessão ao desanexar, mas o Android ainda pode encerrar os processos em
segundo plano. Se aparecer `signal 9`, confira as restrições de bateria do Termux
nas configurações do Android e reinicie a sessão. [Limitações do Termux no Android](https://github.com/termux/termux-app#termux-application)

No Ubuntu, dentro do clone, veja os logs com `tail -n 80 -f data/logs/bot.log`.
Com o suporte ao Termux já incluído no Git, pare o bot com `Ctrl+C` antes de atualizar:

```bash
git pull --ff-only
bash scripts/setup-termux.sh
.venv/bin/python -u main.py
```

`.env`, `settings.env`, `data/database/` e `data/cache/spool/` ficam fora do Git. Para
preservar dados ao mover a instalação, faça backup dessas pastas e arquivos com
o bot parado. Se o backup vier de outro caminho, veja a seção de movimentação do
[guia de operação](OPERACAO.md). Use um token novo para experimentar
o modo público se o bot original continua registrado em uma Bot API local.

## Partes maiores com a Bot API local

Para usar partes de até 500 MB, a aplicação espera o executável Linux
`tools/telegram-bot-api/bin/telegram-bot-api`. Este modo precisa de
`TELEGRAM_API_ID` e `TELEGRAM_API_HASH` no `.env`. Compile para a arquitetura do
Ubuntu seguindo o guia oficial (a etapa `bot-api-build` do [Dockerfile](../Dockerfile)
documenta a revisão usada pelo projeto). A compilação pode consumir bastante
memória e armazenamento no celular. [Instruções oficiais de compilação](https://tdlib.github.io/telegram-bot-api/build.html)

Com o binário no caminho acima, ajuste `TELEGRAM_API_MODE=local` e, por exemplo,
`PART_MAX_MB=300` e `SPOOL_MAX_MB=2048` no `settings.env`. O próprio bot inicia o
servidor na porta padrão 8081. Se estiver mudando o mesmo token da API pública
para a local, siga o procedimento de `logOut` do Telegram antes de ligar a
instância local. [Migração para a Bot API local](https://core.telegram.org/bots/api#logout)
