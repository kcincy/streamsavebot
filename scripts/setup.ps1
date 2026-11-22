. "$PSScriptRoot\common.ps1"
Push-Location $ProjectRoot
try {
    if (-not (Test-Path -LiteralPath $ProjectPython)) {
        if (Test-Path -LiteralPath (Join-Path $ProjectRoot '.venv')) {
            throw 'A pasta .venv ja existe mas esta incompleta. Renomeie-a para preserva-la antes de configurar novamente.'
        }
        if (Get-Command py -ErrorAction SilentlyContinue) {
            Invoke-ProjectNative 'py' @('-3', '-m', 'venv', (Join-Path $ProjectRoot '.venv'))
        } else {
            Invoke-ProjectNative 'python' @('-m', 'venv', (Join-Path $ProjectRoot '.venv'))
        }
        if ($ProjectNativeExitCode -ne 0) { throw 'Nao foi possivel criar o ambiente virtual.' }
    }
    Invoke-ProjectNative $ProjectPython @('-c', "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ necessario'; assert sys.prefix != sys.base_prefix, 'Use o ambiente virtual'")
    if ($ProjectNativeExitCode -ne 0) { throw 'Ambiente virtual invalido; recrie-o conforme docs/OPERACAO.md.' }
    Invoke-ProjectNative $ProjectPython @('-m', 'pip', 'install', '--cache-dir', $env:PIP_CACHE_DIR, '-r', (Join-Path $ProjectRoot 'requirements.txt'))
    if ($ProjectNativeExitCode -ne 0) { throw 'Falha ao instalar dependencias no .venv.' }
    Invoke-ProjectNative $ProjectPython @('-m', 'playwright', 'install', '--no-shell', 'chromium')
    if ($ProjectNativeExitCode -ne 0) { throw 'Falha ao instalar o Chromium dentro de tools/browsers.' }
    foreach ($relative in @('tools\ffmpeg\bin\ffmpeg.exe', 'tools\ffmpeg\bin\ffprobe.exe', 'tools\telegram-bot-api\bin\telegram-bot-api.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $relative))) {
            Write-Warning "Ferramenta local ausente: $relative. Veja docs/OPERACAO.md."
        }
    }
    Write-Host 'Ambiente Python e Chromium configurados dentro do projeto.'
} finally {
    Pop-Location
    Restore-ProjectEnvironment
}
