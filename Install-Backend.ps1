# Install into one app folder. Each download is pinned and checksum-verified;
# progress JSON lets the desktop window show setup as a task while this runs.
[CmdletBinding()]
param(
 [string]$Destination=(Join-Path $env:LOCALAPPDATA 'CaleMedia\AIWorkbench'),
 [ValidateSet('auto','large','compact','light')][string]$Profile='auto',
 [string]$ExtraModels='', [switch]$NoStartup, [switch]$NoLaunch,
 [string]$DownloadCache=''
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'This release requires 64-bit x86 Windows.' }
$Destination=[IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $Destination) {
 if ((Get-ChildItem -LiteralPath $Destination -Force | Select-Object -First 1) -and -not (Test-Path -LiteralPath (Join-Path $Destination '.cale-ai-install'))) { throw 'Choose an empty folder or an existing Cale AI Workbench installation.' }
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$taskLock=New-Object Threading.Mutex($false,('Local\CaleAIInstall-'+([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Destination))).Replace('/','_').Replace('\','_')))
if (-not $taskLock.WaitOne(0)) { throw 'An installer is already running for this folder.' }
try {
 [IO.File]::WriteAllText((Join-Path $Destination '.cale-ai-install'),'Cale AI Workbench 0.4.9')
 $setupState=Join-Path $Destination 'LongTasks/install-progress.json'
 function Write-SetupStatus([int]$percent,[string]$current,[string]$status='working') {
   New-Item -ItemType Directory -Path (Split-Path $setupState) -Force | Out-Null
   $payload=@{percent=$percent;current=$current;status=$status;updated=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()} | ConvertTo-Json -Compress
   $temporary=$setupState+'.'+$PID+'.tmp'
   [IO.File]::WriteAllText($temporary,$payload)
   Move-Item -LiteralPath $temporary -Destination $setupState -Force
   Write-Output "SETUP: $current ($percent%)"
 }
 Write-SetupStatus 2 'Checking this PC and choosing a model'
 if (-not $DownloadCache) { $DownloadCache=Join-Path $Destination 'Downloads' }
 New-Item -ItemType Directory -Path $DownloadCache -Force | Out-Null
 $pins=Get-Content (Join-Path $PSScriptRoot 'downloads.json') -Raw | ConvertFrom-Json
 $profiles=Get-Content (Join-Path $PSScriptRoot 'models/manifest.json') -Raw | ConvertFrom-Json
 $ram=[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB)
 if ($Profile -eq 'auto') { if ($ram -ge 96) {$Profile='large'} elseif ($ram -ge 24) {$Profile='compact'} else {$Profile='light'} }
 $model=$profiles.$Profile
 if ($ram -lt $model.ram_gb) { throw "This model needs about $($model.ram_gb) GB RAM. This PC has $ram GB. Choose a smaller model." }
 $drive=Get-PSDrive -Name ([IO.Path]::GetPathRoot($Destination).Substring(0,1))
 if ($drive.Free -lt (($model.download_gb+32)*1GB)) { throw 'Not enough free disk space for the models, tools and installation cache.' }
 Write-Output "Installing Cale AI Workbench. RAM: $ram GB. Model: $($model.source)."
 function Download-Verified($url,$sha,$name) {
   $target=Join-Path $DownloadCache $name
   if ((Test-Path -LiteralPath $target) -and (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower() -eq $sha.ToLower()) { return $target }
   Write-Output "Downloading $name" | Out-Host
   $partial=$target+'.partial'
   & curl.exe --fail --location --retry 3 --output $partial $url
   if ($LASTEXITCODE -ne 0) { throw "Download failed: $name" }
   if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLower() -ne $sha.ToLower()) { throw "Checksum failed: $name. Download was not executed." }
   Move-Item -LiteralPath $partial -Destination $target -Force
   return $target
 }
 $env:HERMES_HOME=Join-Path $Destination 'data'
 $env:PYTHONUTF8='1'
 New-Item -ItemType Directory -Path (Join-Path $Destination 'Temp') -Force | Out-Null
 $env:TEMP=Join-Path $Destination 'Temp'; $env:TMP=$env:TEMP
 if (-not (Test-Path -LiteralPath (Join-Path $env:HERMES_HOME 'bin/hermes.exe'))) {
   Write-SetupStatus 10 'Installing the agent and Python runtime'
   $bootstrap=Download-Verified "https://raw.githubusercontent.com/NousResearch/hermes-agent/$($pins.hermes_commit)/scripts/install.ps1" $pins.hermes_installer_sha256 'hermes-install.ps1'
   Write-Output 'Installing the agent and its managed Python runtime...'
   & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $bootstrap -HermesHome $env:HERMES_HOME -InstallDir (Join-Path $Destination 'app') -Commit $pins.hermes_commit -NonInteractive -SkipComputerUse -SkipBrowser
   if ($LASTEXITCODE -ne 0) { throw 'Hermes installation failed. Re-run to resume.' }
 }
 $factsFile=Get-ChildItem (Join-Path $env:HERMES_HOME 'installs/*/facts.json') | Sort-Object LastWriteTime -Descending | Select-Object -First 1
 if (-not $factsFile) { throw 'The managed Python runtime was not created.' }
 $facts=Get-Content -LiteralPath $factsFile.FullName -Raw | ConvertFrom-Json
 $runtime=Join-Path $facts.packages.venv.environment 'Scripts/python.exe'
 $box=Join-Path $Destination 'Toolbox'
 Write-SetupStatus 25 'Installing tools and browser support'
 New-Item -ItemType Directory -Path $box -Force | Out-Null
 if (-not (Test-Path -LiteralPath (Join-Path $box 'python/Scripts/python.exe'))) { & $runtime -m venv (Join-Path $box 'python'); if ($LASTEXITCODE -ne 0) { throw 'Could not create the tool runtime.' } }
 $python=Join-Path $box 'python/Scripts/python.exe'
 Write-Output 'Installing document, database, testing and model-connection tools...'
 & $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-windows.txt')
 if ($LASTEXITCODE -ne 0) { throw 'Python tool installation failed.' }
 if (-not (Test-Path -LiteralPath (Join-Path $box 'node-v26.13.0-win-x64/node.exe'))) {
   $archive=Download-Verified $pins.node.url $pins.node.sha256 'node-v26.13.0-win-x64.zip'
   Expand-Archive -LiteralPath $archive -DestinationPath $box -Force
 }
 $node=Join-Path $box 'node-v26.13.0-win-x64/node.exe'
 $githubCli=Join-Path $box 'gh_2.103.3_windows_amd64/bin/gh.exe'
 if (-not (Test-Path -LiteralPath $githubCli)) {
   $archive=Download-Verified $pins.github_cli.url $pins.github_cli.sha256 'gh_2.103.3_windows_amd64.zip'
   Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $box 'gh_2.103.3_windows_amd64') -Force
   if (-not (Test-Path -LiteralPath $githubCli)) { throw 'GitHub CLI installation failed.' }
 }
 $env:PATH=(Split-Path $node)+';'+(Join-Path $box 'python/Scripts')+';'+$env:PATH
 Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'package.json') -Destination $box -Force
 Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'package-lock.json') -Destination $box -Force
 Push-Location $box
 try {
   & (Join-Path (Split-Path $node) 'npm.cmd') ci --ignore-scripts --no-audit --no-fund
   if ($LASTEXITCODE -ne 0) { throw 'JavaScript tool installation failed.' }
   & $node (Join-Path $box 'node_modules/playwright/cli.js') install chromium
   if ($LASTEXITCODE -ne 0) { throw 'Browser download failed.' }
 } finally { Pop-Location }
 foreach ($entry in @(@{Name='java';Folder='jdk-21.0.13+9';Archive='jdk.zip'},@{Name='ghidra';Folder='ghidra_12.1.4_PUBLIC';Archive='ghidra-12.1.4.zip'})) {
   if (-not (Test-Path -LiteralPath (Join-Path $box $entry.Folder))) {
     $pin=$pins.($entry.Name); $archive=Download-Verified $pin.url $pin.sha256 $entry.Archive
     Expand-Archive -LiteralPath $archive -DestinationPath $box -Force
   }
 }
 # Copy only published source. Never touch project folders, saved tasks or account credentials.
 $manifest=Get-Content (Join-Path $PSScriptRoot 'install-files.json') -Raw | ConvertFrom-Json
 foreach ($file in $manifest) {
   $target=Join-Path $Destination $file
   New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
   Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $target -Force
 }
 if (-not $NoLaunch) {
   Start-Process (Join-Path $box 'python/Scripts/pythonw.exe') -ArgumentList ('"'+(Join-Path $Destination 'long_tasks.py')+'"') -WindowStyle Normal
 }
 Write-SetupStatus 42 'Installing local image and voice tools'
 $env:GHIDRA_INSTALL_DIR=Join-Path $box 'ghidra_12.1.4_PUBLIC'; $env:JAVA_HOME=Join-Path $box 'jdk-21.0.13+9'
 $env:NODE_PATH=Join-Path $box 'node_modules'
 $env:PATH=(Join-Path $box 'node_modules/.bin')+';'+$env:PATH
 $media=Join-Path $Destination 'Media'
 New-Item -ItemType Directory -Path $media -Force | Out-Null
 if (-not (Test-Path -LiteralPath (Join-Path $media 'python/Scripts/python.exe'))) {
   $uvArchive=Download-Verified $pins.uv.url $pins.uv.sha256 'uv-win64.zip'
   $uvFolder=Join-Path $media 'uv'
   New-Item -ItemType Directory -Path $uvFolder -Force | Out-Null
   Expand-Archive -LiteralPath $uvArchive -DestinationPath $uvFolder -Force
   $uv=Join-Path $uvFolder 'uv.exe'
   $env:UV_PYTHON_INSTALL_DIR=Join-Path $media 'runtime'
   $env:UV_CACHE_DIR=Join-Path $media 'uv-cache'
   & $uv python install '3.13'
   if ($LASTEXITCODE -ne 0) { throw 'The media Python runtime failed to install.' }
   & $uv venv --python '3.13' --seed (Join-Path $media 'python')
   if ($LASTEXITCODE -ne 0) { throw 'The media environment failed to initialize.' }
 }
 $mediaPython=Join-Path $media 'python/Scripts/python.exe'
 Write-Output 'Installing local image, voice, vision, video and training tools...'
 & $mediaPython -m pip install --disable-pip-version-check --extra-index-url https://download.pytorch.org/whl/cu128 -r (Join-Path $Destination 'media-requirements.txt')
 if ($LASTEXITCODE -ne 0) { throw 'Media tool installation failed.' }
 try { $null=Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 }
 catch {
   Write-SetupStatus 54 'Installing and starting Ollama'
   $ollama=Join-Path $env:LOCALAPPDATA 'Programs/Ollama/ollama.exe'
   if (-not (Test-Path -LiteralPath $ollama)) {
     # The native Windows app handles later Ollama updates; the old CLI ZIP does not.
     $installer=Download-Verified $pins.ollama_setup.url $pins.ollama_setup.sha256 'OllamaSetup.exe'
     & $installer /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
     if ($LASTEXITCODE -ne 0) { throw 'Ollama native installation failed.' }
     if (-not (Test-Path -LiteralPath $ollama)) { throw 'The Ollama installer did not create its application.' }
   }
   try { $null=Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 }
   catch { Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden }
   for ($attempt=0;$attempt -lt 30;$attempt++) { Start-Sleep -Seconds 1; try { $null=Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2; break } catch { if ($attempt -eq 29) {throw 'Ollama did not start.'} } }
 }
 Write-Output "Downloading model files for $($model.source). This is the largest step."
 Write-SetupStatus 60 'Downloading your main AI model'
 & $python (Join-Path $Destination 'configure_install.py') --root $Destination --profile $Profile
 if ($LASTEXITCODE -ne 0) { throw 'Model/configuration setup failed.' }
 Write-Output 'Downloading local image, speech and small training models...'
 Write-SetupStatus 80 'Downloading a natural voice, image, speech, and training models'
 & $mediaPython (Join-Path $Destination 'media_worker.py') --download-models
 if ($LASTEXITCODE -ne 0) { throw 'Media model download failed. Re-run to resume.' }
 if ($ExtraModels) {
   foreach ($extraModel in $ExtraModels.Split(',')) {
     & $python (Join-Path $Destination 'model_library.py') $extraModel
     if ($LASTEXITCODE -ne 0) { throw "Optional model failed: $extraModel. Re-run to resume." }
   }
 }
 Write-Output 'Checking the installed companion...'
 Write-SetupStatus 91 'Checking that everything works'
 & $python (Join-Path $Destination 'Validation/test_cloud_ai.py')
 if ($LASTEXITCODE -ne 0) { throw 'Cloud-routing behavior checks failed.' }
 & $python (Join-Path $Destination 'Validation/test_chatgpt_auth.py')
 if ($LASTEXITCODE -ne 0) { throw 'ChatGPT connection behavior checks failed.' }
 & $python (Join-Path $Destination 'Validation/test_long_tasks.py')
 if ($LASTEXITCODE -ne 0) { throw 'Long-task behavior checks failed.' }
 & $python (Join-Path $Destination 'Validation/test_model_library.py')
 if ($LASTEXITCODE -ne 0) { throw 'Automatic model selection checks failed.' }
 & $mediaPython (Join-Path $Destination 'Validation/test_media.py')
 if ($LASTEXITCODE -ne 0) { throw 'Installed media checks failed.' }
 if (-not $NoStartup) { & (Join-Path $Destination 'Install Long Tasks Startup.ps1'); Start-ScheduledTask -TaskName 'CaleAI_LongTasks' -ErrorAction SilentlyContinue }
 Write-SetupStatus 100 'Your AI is ready' 'complete'
 # Optional local models continue in a separate process after the installer closes.
 Start-Process -FilePath (Join-Path $box 'python/Scripts/pythonw.exe') -ArgumentList ('"'+(Join-Path $Destination 'model_library.py')+'" --auto --status-dir "'+(Join-Path $Destination 'LongTasks')+'"') -WindowStyle Hidden
 Write-Output "Installation complete: $Destination"
} catch {
 if ($setupState) { Write-SetupStatus 0 ("Setup needs attention: " + $_.Exception.Message) 'error' }
 throw
} finally { $taskLock.ReleaseMutex(); $taskLock.Dispose() }
