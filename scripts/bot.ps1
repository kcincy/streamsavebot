. "$PSScriptRoot\common.ps1"
Push-Location $ProjectRoot
try {
    Assert-ProjectPython
    Invoke-ProjectNative $ProjectPython @('-B', (Join-Path $ProjectRoot 'main.py'))
    $commandExit = $ProjectNativeExitCode
} finally {
    Pop-Location
    Restore-ProjectEnvironment
}
exit $commandExit
