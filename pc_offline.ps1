# Turn this PC's currently active physical network adapters off, or restore
# only the adapters recorded by this app. Windows elevation is required.
param([ValidateSet('offline','online')][string]$Mode)
$ErrorActionPreference='Stop'
$state=Join-Path $PSScriptRoot 'LongTasks/offline-network.json'
New-Item -ItemType Directory -Path (Split-Path $state) -Force | Out-Null

if ($Mode -eq 'offline') {
    if (Test-Path -LiteralPath $state) { exit 0 }
    $adapters=@(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' })
    if (-not $adapters) { throw 'No active physical network adapter was found.' }
    $record=@{ names=@($adapters | ForEach-Object { $_.Name }); created=(Get-Date).ToString('o') }
    $record | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $state -Encoding UTF8
    try {
        foreach ($adapter in $adapters) {
            Disable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction Stop
        }
    } catch {
        foreach ($adapter in $adapters) {
            Enable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $state -Force -ErrorAction SilentlyContinue
        throw
    }
} else {
    if (-not (Test-Path -LiteralPath $state)) { exit 0 }
    $record=Get-Content -LiteralPath $state -Raw | ConvertFrom-Json
    foreach ($name in @($record.names)) {
        if (Get-NetAdapter -Name $name -ErrorAction SilentlyContinue) {
            Enable-NetAdapter -Name $name -Confirm:$false -ErrorAction Stop
        }
    }
    Remove-Item -LiteralPath $state -Force
}
