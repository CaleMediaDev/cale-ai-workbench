# Launch one interactive Hermes task using the tools installed with this app.
param(
    [string]$Folder = (Join-Path $PSScriptRoot 'Projects'),
    [string]$Task,
    [switch]$PickFolder,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$env:HERMES_HOME = Join-Path $PSScriptRoot 'data'
$env:PYTHONUTF8 = '1'
$taskTemp = Join-Path $PSScriptRoot 'Temp'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP = $taskTemp
$env:TMP = $taskTemp
$taskToolbox = Join-Path $PSScriptRoot 'Toolbox'
$env:PATH = (Join-Path $taskToolbox 'node-v26.13.0-win-x64') + ';' + (Join-Path $taskToolbox 'node_modules/.bin') + ';' + (Join-Path $taskToolbox 'python/Scripts') + ';' + $env:PATH
$env:GHIDRA_INSTALL_DIR = Join-Path $taskToolbox 'ghidra_12.1.4_PUBLIC'
$env:JAVA_HOME = Join-Path $taskToolbox 'jdk-21.0.13+9'
$taskBrowserModules = Join-Path $env:APPDATA 'npm/node_modules/@playwright/mcp/node_modules'
$env:NODE_PATH = (Join-Path $taskToolbox 'node_modules') + [IO.Path]::PathSeparator + $env:NODE_PATH
if (Test-Path -LiteralPath $taskBrowserModules) {
    $env:NODE_PATH = $taskBrowserModules + [IO.Path]::PathSeparator + $env:NODE_PATH
}
$taskEngine = Join-Path $env:HERMES_HOME 'bin/hermes.exe'
if (-not (Test-Path -LiteralPath $taskEngine)) { throw 'The Hermes installation is missing.' }
$taskFactsFile = Get-ChildItem (Join-Path $env:HERMES_HOME 'installs/*/facts.json') |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $taskFactsFile) { throw 'The Hermes runtime information is missing.' }
$taskFacts = Get-Content -LiteralPath $taskFactsFile.FullName -Raw | ConvertFrom-Json
$taskPython = Join-Path $taskFacts.packages.venv.environment 'Scripts/python.exe'
$taskSettingsJson = & $taskPython (Join-Path $PSScriptRoot 'read-settings.py')
if ($LASTEXITCODE -ne 0) { throw 'Local-model configuration validation failed.' }
$taskSettings = $taskSettingsJson | ConvertFrom-Json
$env:LOCAL_AI_ROUTER_KEY = & (Join-Path $taskToolbox 'python/Scripts/python.exe') (Join-Path $PSScriptRoot 'cloud_ai.py') --ensure-token
if ($LASTEXITCODE -ne 0) { throw 'The local/free-cloud router could not start.' }
if ($PickFolder) {
    Add-Type -AssemblyName System.Windows.Forms
    $taskDialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $taskDialog.Description = 'Choose the folder you want Local AI to work in'
    if (Test-Path -LiteralPath $Folder) { $taskDialog.SelectedPath = $Folder }
    try {
        if ($taskDialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return }
        $Folder = $taskDialog.SelectedPath
        Write-Host "Selected folder: $Folder"
    } finally { $taskDialog.Dispose() }
}
try { $taskModels = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 }
catch {
    $taskOllama = Join-Path $env:LOCALAPPDATA 'Programs/Ollama/ollama.exe'
    if (-not (Test-Path -LiteralPath $taskOllama)) { $taskOllama = Join-Path $taskToolbox 'ollama/ollama.exe' }
    if (-not (Test-Path -LiteralPath $taskOllama)) { throw 'Ollama was not found.' }
    Write-Host 'Starting your local model service...'
    Start-Process -FilePath $taskOllama -ArgumentList 'serve' -WindowStyle Hidden
    $taskModels = $null
    for ($taskAttempt = 0; $taskAttempt -lt 15; $taskAttempt++) {
        Start-Sleep -Seconds 1
        try { $taskModels = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2; break } catch {}
    }
    if (-not $taskModels) { throw 'Ollama did not start. Open Ollama and try again.' }
}
$taskModelName = $taskSettings.model
if ($taskModelName -notmatch ':') { $taskModelName += ':latest' }
if (-not ($taskModels.models.name -contains $taskModelName)) {
    throw "The configured local model is missing from Ollama: $taskModelName"
}
$taskModelInfo = Invoke-RestMethod 'http://127.0.0.1:11434/api/show' -Method Post -ContentType 'application/json' -Body (@{model=$taskModelName} | ConvertTo-Json) -TimeoutSec 10
if ($taskModelInfo.remote_host -or $taskModelInfo.remote_model) { throw 'This launcher requires local model weights.' }
Write-Host "LOCAL AI - Hermes | $taskModelName | verification enabled" -ForegroundColor Cyan
Write-Host 'Local Qwen, or connected free cloud with local fallback. No Codex or paid AI routes.'
& (Join-Path $taskToolbox 'python/Scripts/python.exe') (Join-Path $PSScriptRoot 'cloud_ai.py') --status
if ($Check) {
    & $taskEngine mcp test localweb
    if ($LASTEXITCODE -ne 0) { throw 'Public web tools failed to connect.' }
    & $taskEngine mcp test pages
    if ($LASTEXITCODE -ne 0) { throw 'Browser tools failed to connect.' }
    & $taskEngine mcp test toolbox
    if ($LASTEXITCODE -ne 0) { throw 'Specialist tools failed to connect.' }
    return
}
New-Item -ItemType Directory -Force -Path $Folder | Out-Null
$Folder = (Resolve-Path -LiteralPath $Folder).Path
$taskRules = Join-Path $Folder 'AGENTS.md'
if (-not (Test-Path -LiteralPath $taskRules)) {
    # A project marker lets Hermes track edits and whether checks are still current.
    # Existing project instructions are always preserved.
    [IO.File]::WriteAllText($taskRules, @'
# Local AI workspace

Complete the requested work and verify the actual result. Preserve source data and existing tests. After the last relevant code edit, run appropriate tests or exercise the changed behavior. Report what passed, what failed, and anything unverified. Use official live sources for current facts. Use only the user's own direct website connections; do not use third-party website gateways. Use only the configured local/free-cloud AI route. Do not invoke paid AI or Codex.
'@, [Text.UTF8Encoding]::new($false))
}
Write-Host "Working folder: $Folder"
Push-Location -LiteralPath $Folder
try {
    if ($Task) { & $taskEngine chat --cli --oneshot -q $Task }
    else { & $taskEngine --tui }
    if ($LASTEXITCODE -ne 0) { throw "Hermes exited with code $LASTEXITCODE." }
} finally { Pop-Location }
