# Ambiente local - Windows x64

Requisitos de sistema: Python 3.10+ x64 e PowerShell. O Python base sustenta o ambiente virtual;
nao remova a instalacao base. Os pacotes do projeto sao instalados exclusivamente em `.venv`.

```powershell
.\scripts\setup.ps1
python main.py
```

O setup usa requirements.txt, instala Chromium em tools/browsers e mantem cache pip e temporarios
em cache/. FFmpeg e FFprobe ficam em tools/ffmpeg/bin. O servidor oficial e suas DLLs ficam em
tools/telegram-bot-api/bin. Nao e necessario iniciar a API separadamente nem ativar o venv manualmente.
A configuracao operacional fica em `settings.env`; copie `.env.telegram.example` para `.env`
e preencha somente as credenciais. Consulte [README.md](README.md).

Se a politica do PowerShell bloquear o setup, use:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

Para instalar somente os pacotes Python:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --cache-dir cache\pip-cache -r requirements.txt
```

## Mover ou reinstalar

Encerre o processo principal e copie o projeto incluindo `.env`, `settings.env`, database/, cache/spool/ se houver
pendencias, tools/ e logs/ se desejar o historico. Nao copie apenas os arquivos Python. O banco e
copiado com o processo fechado, incluindo eventuais auxiliares -wal/-shm ainda existentes.

O venv nao e portavel: se tiver sido copiado, renomeie-o para .venv.old e execute setup.ps1 no destino.
O setup nao remove um ambiente anterior. Os executaveis empacotados sao Windows x64.

Caminhos de midia pendente estao no banco. Se mudar a raiz do projeto, use a funcao
storage.relocate_media_paths com o processo parado para atualizar as referencias do spool.
Na reorganizacao atual isso foi executado e testado automaticamente, sem alterar IDs ou destinatarios.

## Binarios

FFmpeg/FFprobe: [distribuicoes Windows](https://ffmpeg.org/download.html#build-windows).
Bot API: [repositorio oficial](https://github.com/tdlib/telegram-bot-api) e
[gerador oficial de build](https://tdlib.github.io/telegram-bot-api/build.html).
Nao e necessario guardar source, vcpkg e objetos de compilacao para executar o servidor ja pronto.
Licencas e DLLs de runtime foram mantidas junto aos executaveis.

Esta instalacao ja utiliza a API local. Nao execute logOut na API publica a cada inicio. Uma migracao
nova da API publica exige seguir o procedimento oficial antes de ativar o servidor local.
