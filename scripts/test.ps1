. "$PSScriptRoot\common.ps1"
Push-Location $ProjectRoot
try {
    Assert-ProjectPython
    Invoke-ProjectNative $ProjectPython @('-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py')
    if ($ProjectNativeExitCode -ne 0) { exit $ProjectNativeExitCode }
    Invoke-ProjectNative $ProjectPython @('-B', 'tests/test_concat.py')
    if ($ProjectNativeExitCode -ne 0) { exit $ProjectNativeExitCode }
    $ProjectNode = Join-Path $ProjectRoot '.venv\Lib\site-packages\playwright\driver\node.exe'
    Invoke-ProjectNative $ProjectNode @('tests/test_recorder.cjs')
    $commandExit = $ProjectNativeExitCode
} finally {
    Pop-Location
    Restore-ProjectEnvironment
}
exit $commandExit
