$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProjectPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ProjectEnvironmentBefore = @{}
$ProjectNativeExitCode = 0
foreach ($name in @('TEMP', 'TMP', 'PIP_CACHE_DIR', 'PLAYWRIGHT_BROWSERS_PATH', 'PLAYWRIGHT_SKIP_BROWSER_GC', 'PYTHONUTF8', 'PYTHONPATH', 'PATH')) {
    $ProjectEnvironmentBefore[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$ProjectTemporary = Join-Path $ProjectRoot 'cache\tmp'
New-Item -ItemType Directory -Force -Path $ProjectTemporary | Out-Null
$env:TEMP = $ProjectTemporary
$env:TMP = $ProjectTemporary
$env:PIP_CACHE_DIR = Join-Path $ProjectRoot 'cache\pip-cache'
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ProjectRoot 'tools\browsers'
$env:PLAYWRIGHT_SKIP_BROWSER_GC = '1'
$env:PYTHONUTF8 = '1'
$env:PATH = "$(Join-Path $ProjectRoot 'tools\ffmpeg\bin');$env:PATH"

function Assert-ProjectPython {
    if (-not (Test-Path -LiteralPath $ProjectPython)) {
        throw 'Ambiente virtual ausente. Execute .\scripts\setup.ps1 primeiro.'
    }
}

function Restore-ProjectEnvironment {
    foreach ($name in $ProjectEnvironmentBefore.Keys) {
        [Environment]::SetEnvironmentVariable($name, $ProjectEnvironmentBefore[$name], 'Process')
    }
}

function Invoke-ProjectNative {
    param([string]$Program, [string[]]$Arguments)
    Get-Command $Program -ErrorAction Stop | Out-Null
    # PowerShell 5 transforma stderr redirecionado em ErrorRecord, inclusive avisos.
    $ErrorActionPreference = 'Continue'
    & $Program @Arguments 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { $_ }
    }
    Set-Variable -Name ProjectNativeExitCode -Value $LASTEXITCODE -Scope 1
}
