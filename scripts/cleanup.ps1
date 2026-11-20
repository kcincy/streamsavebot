# Limpeza manual: as exclusoes recursivas foram bloqueadas na execucao do agente.
# Nao e chamado pelo bot e nunca inclui banco atual, fila, configuracao ou logs ativos.
$ErrorActionPreference = 'Stop'
$project = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$legacy = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'SuperLiveTelegramBotAPI'))
if (-not (Test-Path -LiteralPath (Join-Path $project 'database\backups\before-layout.sqlite3'))) {
    throw 'Backup anterior a reorganizacao ausente. Limpeza cancelada.'
}
$oldRuntime = Join-Path $project '.runtime'
$usingOld = @(Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($legacy+'\',[StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and $_.CommandLine.Contains($oldRuntime))
})
if ($usingOld.Count) { throw 'Uma instalacao antiga ainda esta em uso. Limpeza cancelada.' }
$allowed = @('build','build-vs','vcpkg','source','runtime','logs')
$targets = @($allowed | ForEach-Object { Join-Path $legacy $_ })
$targets += @('obsolete-runtime','obsolete-bytecode','obsolete-recordings') | ForEach-Object {
    Join-Path (Join-Path $project 'cache') $_
}
$freed = [long]0
foreach ($target in $targets) {
    if (-not (Test-Path -LiteralPath $target)) { continue }
    $resolved = (Resolve-Path -LiteralPath $target).ProviderPath
    $parent = [IO.Path]::GetDirectoryName($resolved)
    $name = [IO.Path]::GetFileName($resolved)
    $valid = ($parent -eq $legacy -and $name -in $allowed) -or
             ($parent -eq (Join-Path $project 'cache') -and $name -in @('obsolete-runtime','obsolete-bytecode','obsolete-recordings'))
    if (-not $valid) { throw 'Alvo fora da lista restrita. Limpeza cancelada.' }
    if ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Alvo e um link. Confira-o manualmente.'
    }
    $size = Get-ChildItem -LiteralPath $resolved -Recurse -File -Force | Measure-Object Length -Sum
    Remove-Item -LiteralPath $resolved -Recurse -Force
    $freed += [long]$size.Sum
    Write-Output "Removido: $name"
}
$report = Join-Path $project 'cache\cleanup-report.json'
if (Test-Path -LiteralPath $report) {
    $entries = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    foreach ($entry in $entries) {
        if ($entry.Path -in $targets -and -not (Test-Path -LiteralPath $entry.Path)) { $entry.Status = 'removed_manually' }
    }
    $entries | ConvertTo-Json | Set-Content -LiteralPath $report -Encoding UTF8
}
Write-Output ("Bytes removidos: " + $freed)
