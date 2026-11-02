# Install into one app folder. Each download is pinned and checksum-verified;
# progress JSON lets the desktop window show setup as a task while this runs.
[CmdletBinding()]
param(
 [string]$Destination='',
 [ValidateSet('auto','large','compact','light')][string]$Profile='auto',
 [string]$ExtraModels='', [switch]$NoStartup, [switch]$NoLaunch,
 [string]$DownloadCache=''
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
. (Join-Path $PSScriptRoot 'Install-Environment.ps1')
Initialize-CalePowerShellEnvironment
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'This release requires 64-bit x86 Windows.' }
$Destination=Get-CaleInstallDestination $Destination
if (Test-Path -LiteralPath $Destination) {
 if ((Get-ChildItem -LiteralPath $Destination -Force | Select-Object -First 1) -and -not (Test-Path -LiteralPath (Join-Path $Destination '.cale-ai-install'))) { throw 'Choose an empty folder or an existing Cale AI Workbench installation.' }
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$taskLock=Enter-CaleInstallLock $Destination
try {
 $script:installerStarted=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
 # Keep recovery metadata beside the install from its first step. The source
 # updater also copies this file separately; the manifest cannot list itself.
 $sourceManifest=Join-Path $PSScriptRoot 'install-files.json'
 $targetManifest=Join-Path $Destination 'install-files.json'
 if ([IO.Path]::GetFullPath($sourceManifest) -ne [IO.Path]::GetFullPath($targetManifest)) {
   Copy-Item -LiteralPath $sourceManifest -Destination $targetManifest -Force
 }
 # The version becomes official only after setup and its checks succeed.
 [IO.File]::WriteAllText((Join-Path $Destination '.cale-ai-install'),'Cale AI Workbench setup pending')
 $setupState=Join-Path $Destination 'LongTasks/install-progress.json'
 function Write-SetupStatus([int]$percent,[string]$current,[string]$status='working') {
   New-Item -ItemType Directory -Path (Split-Path $setupState) -Force | Out-Null
   $payload=@{percent=$percent;current=$current;status=$status;updated=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();pid=$PID;installer_started=$script:installerStarted} | ConvertTo-Json -Compress
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
 $vram=0
 try {
   $gpuLines=& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null
   if ($LASTEXITCODE -eq 0) { foreach ($line in $gpuLines) { if ($line -match '^\s*(\d+)\s*$') { $vram += [int]$Matches[1] } } }
 } catch { }
 $vramGB=$vram/1024
 if ($Profile -eq 'auto') { $Profile=Select-CaleModelProfile $ram $vramGB }
 $model=$profiles.$Profile
 if ($ram -lt $model.ram_gb) { throw "This model needs about $($model.ram_gb) GB RAM. This PC has $ram GB. Choose a smaller model." }
 $drive=Get-PSDrive -Name ([IO.Path]::GetPathRoot($Destination).Substring(0,1))
 if ($drive.Free -lt (($model.download_gb+32)*1GB)) { throw 'Not enough free disk space for the models, tools and installation cache.' }
 Write-Output "Installing Cale AI Workbench. RAM: $ram GB. Model: $($model.source)."
 function Download-Verified($url,$sha,$name) {
   $target=Join-Path $DownloadCache $name
   if ((Test-Path -LiteralPath $target) -and (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower() -eq $sha.ToLower()) { return $target }
   $stage=2
   if (Test-Path -LiteralPath $setupState) {
     try { $stage=[int]((Get-Content -LiteralPath $setupState -Raw | ConvertFrom-Json).percent) } catch { }
   }
   Write-SetupStatus $stage ("Downloading and checking " + $name) | Out-Host
   Write-Output "Downloading $name" | Out-Host
   $partial=$target+'.partial'
   if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
     & curl.exe --fail --location --retry 3 --output $partial $url
     if ($LASTEXITCODE -ne 0) { throw "Download failed: $name" }
   } else {
     # Some clean or locked-down Windows images omit curl.exe. PowerShell's
     # built-in downloader still gets the same SHA-256 verification below.
     Invoke-WebRequest -Uri $url -OutFile $partial -UseBasicParsing
   }
   if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLower() -ne $sha.ToLower()) { throw "Checksum failed: $name. Download was not executed." }
   Move-Item -LiteralPath $partial -Destination $target -Force
   return $target
 }
 $env:HERMES_HOME=Join-Path $Destination 'data'
 $env:PYTHONUTF8='1'
 New-Item -ItemType Directory -Path (Join-Path $Destination 'Temp') -Force | Out-Null
 $env:TEMP=Join-Path $Destination 'Temp'; $env:TMP=$env:TEMP
 $runtime=Find-CaleHermesPython $env:HERMES_HOME
 if (-not (Test-CaleRunnable (Join-Path $env:HERMES_HOME 'bin/hermes.exe')) -or
     -not $runtime -or -not (Test-CaleRunnable $runtime @('-V'))) {
   Write-SetupStatus 10 'Installing the agent and Python runtime'
   $bootstrap=Download-Verified "https://raw.githubusercontent.com/NousResearch/hermes-agent/$($pins.hermes_commit)/scripts/install.ps1" $pins.hermes_installer_sha256 'hermes-install.ps1'
   Write-Output 'Installing or repairing the agent and its managed Python runtime...'
   & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $bootstrap -HermesHome $env:HERMES_HOME -InstallDir (Join-Path $Destination 'app') -Commit $pins.hermes_commit -NonInteractive -SkipComputerUse -SkipBrowser
   if ($LASTEXITCODE -ne 0) { throw 'Hermes installation failed. Re-run to resume.' }
   $runtime=Find-CaleHermesPython $env:HERMES_HOME
 }
 if (-not $runtime -or -not (Test-CaleRunnable $runtime @('-V'))) { throw 'The agent Python runtime did not start after repair.' }
 $box=Join-Path $Destination 'Toolbox'
 Write-SetupStatus 25 'Installing tools and browser support'
 New-Item -ItemType Directory -Path $box -Force | Out-Null
 $toolPython=Join-Path $box 'python/Scripts/python.exe'
 if (-not (Test-CaleRunnable $toolPython @('-V'))) {
   Reset-CaleManagedDirectory $Destination (Join-Path $box 'python')
   & $runtime -m venv (Join-Path $box 'python')
   if ($LASTEXITCODE -ne 0) { throw 'Could not create the tool runtime.' }
 }
 $python=Join-Path $box 'python/Scripts/python.exe'
 if (-not (Test-CaleRunnable $python @('-V'))) { throw 'The tool Python runtime did not start.' }
 Write-SetupStatus 26 'Installing document, database and file tools'
 Write-Output 'Installing document, database, testing and model-connection tools...'
 & $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-windows.txt')
 if ($LASTEXITCODE -ne 0) { throw 'Python tool installation failed.' }
 # Crawl4AI uses Python Playwright, separate from the JavaScript MCP browser.
 Write-SetupStatus 29 'Downloading the local browser tools'
 & $python -m playwright install chromium --only-shell
 if ($LASTEXITCODE -ne 0) { throw 'Local crawler browser download failed.' }
 if (-not (Test-CaleRunnable (Join-Path $box 'node-v26.13.0-win-x64/node.exe'))) {
   $archive=Download-Verified $pins.node.url $pins.node.sha256 'node-v26.13.0-win-x64.zip'
   Expand-Archive -LiteralPath $archive -DestinationPath $box -Force
 }
 $node=Join-Path $box 'node-v26.13.0-win-x64/node.exe'
 if (-not (Test-CaleRunnable $node)) { throw 'The managed Node.js runtime did not start.' }
 $githubCli=Join-Path $box 'gh_2.103.3_windows_amd64/bin/gh.exe'
 if (-not (Test-CaleRunnable $githubCli)) {
   $archive=Download-Verified $pins.github_cli.url $pins.github_cli.sha256 'gh_2.103.3_windows_amd64.zip'
   Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $box 'gh_2.103.3_windows_amd64') -Force
   if (-not (Test-CaleRunnable $githubCli)) { throw 'GitHub CLI installation failed.' }
 }
 $env:PATH=(Split-Path $node)+';'+(Join-Path $box 'python/Scripts')+';'+$env:PATH
 $installedLock=Join-Path $box 'package-lock.json'
 $sourceLock=Join-Path $PSScriptRoot 'package-lock.json'
 $reuseNode=(Test-Path -LiteralPath $installedLock -PathType Leaf) -and ((Get-FileHash -LiteralPath $installedLock -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $sourceLock -Algorithm SHA256).Hash)
 foreach ($module in @('rea-agents','chrome-devtools-mcp','@playwright/mcp','@ast-grep/cli')) {
   if (-not (Test-Path -LiteralPath (Join-Path $box "node_modules/$module/package.json") -PathType Leaf)) { $reuseNode=$false }
 }
 Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'package.json') -Destination $box -Force
 Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'package-lock.json') -Destination $box -Force
 Push-Location $box
 try {
   if ($reuseNode) { Write-Output 'JavaScript tools are already current; keeping the running browser and agent tools.' }
   else {
     Write-SetupStatus 32 'Installing browser and design tools'
     & (Join-Path (Split-Path $node) 'npm.cmd') ci --ignore-scripts --no-audit --no-fund
     if ($LASTEXITCODE -ne 0) { throw 'JavaScript tool installation failed.' }
   }
   # npm's package files can survive an interrupted browser download. The
   # Playwright command is idempotent and restores the binary on every retry.
   & $node (Join-Path $box 'node_modules/playwright/cli.js') install chromium
   if ($LASTEXITCODE -ne 0) { throw 'Browser download failed.' }
 } finally { Pop-Location }
 foreach ($entry in @(@{Name='java';Folder='jdk-21.0.13+9';Archive='jdk.zip';Probe='bin/java.exe'},@{Name='ghidra';Folder='ghidra_12.1.4_PUBLIC';Archive='ghidra-12.1.4.zip';Probe='ghidraRun.bat'})) {
   $toolFolder=Join-Path $box $entry.Folder
   $toolProbe=Join-Path $toolFolder $entry.Probe
   $ready=if ($entry.Name -eq 'java') { Test-CaleRunnable $toolProbe @('-version') } else { Test-Path -LiteralPath $toolProbe -PathType Leaf }
   if (-not $ready) {
     Write-SetupStatus 36 ("Adding " + $entry.Folder + " tools")
     $pin=$pins.($entry.Name); $archive=Download-Verified $pin.url $pin.sha256 $entry.Archive
     Expand-Archive -LiteralPath $archive -DestinationPath $box -Force
     $ready=if ($entry.Name -eq 'java') { Test-CaleRunnable $toolProbe @('-version') } else { Test-Path -LiteralPath $toolProbe -PathType Leaf }
     if (-not $ready) { throw "$($entry.Folder) did not install correctly." }
   }
 }
 # Copy only published source. Never touch project folders, saved tasks or account credentials.
 $manifest=Get-Content (Join-Path $PSScriptRoot 'install-files.json') -Raw | ConvertFrom-Json
 foreach ($file in $manifest) {
   $source=Join-Path $PSScriptRoot $file
   $target=Join-Path $Destination $file
   New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
   if ([IO.Path]::GetFullPath($source) -ne [IO.Path]::GetFullPath($target)) {
     Copy-Item -LiteralPath $source -Destination $target -Force
   }
 }
 & $python (Join-Path $Destination 'skill_catalog.py')
 if ($LASTEXITCODE -ne 0) { throw 'Local skill setup failed.' }
 Write-SetupStatus 38 'Checking and installing FFmpeg media tools'
 & $python (Join-Path $Destination 'ffmpeg_runtime.py') --ensure
 if ($LASTEXITCODE -ne 0) { throw 'FFmpeg installation or version check failed.' }
 $env:PATH=(Join-Path $box 'bin')+';'+$env:PATH
 # Prepare signed-in Chrome control locally. Chrome itself asks the user to
 # approve its extension and explicitly choose which tabs Cale may access.
 $env:PATH=(Join-Path $env:HERMES_HOME 'bin')+';'+$env:PATH
 & $runtime (Join-Path $Destination 'vendor/hermes-connector/install_companion.py') --source (Join-Path $Destination 'vendor/hermes-connector/plugin') --hermes-home $env:HERMES_HOME --no-show-code
 if ($LASTEXITCODE -ne 0) { throw 'Local Chrome companion installation failed.' }
 New-Item -ItemType Directory -Path (Join-Path $Destination 'Updates') -Force | Out-Null
 [IO.File]::WriteAllText((Join-Path $Destination 'Updates/features-0.5.16.done'),'Crawl4AI 0.9.5; Hermes Connector 0.2.6')
 if (-not $NoLaunch) {
   Start-Process (Join-Path $box 'python/Scripts/pythonw.exe') -ArgumentList ('"'+(Join-Path $Destination 'long_tasks.py')+'"') -WindowStyle Normal
 }
 Write-SetupStatus 42 'Installing local image and voice tools'
 $env:GHIDRA_INSTALL_DIR=Join-Path $box 'ghidra_12.1.4_PUBLIC'; $env:JAVA_HOME=Join-Path $box 'jdk-21.0.13+9'
 $env:NODE_PATH=Join-Path $box 'node_modules'
 $env:PATH=(Join-Path $box 'node_modules/.bin')+';'+$env:PATH
 $media=Join-Path $Destination 'Media'
 New-Item -ItemType Directory -Path $media -Force | Out-Null
 if (-not (Test-CaleRunnable (Join-Path $media 'python/Scripts/python.exe') @('-V'))) {
   Reset-CaleManagedDirectory $Destination (Join-Path $media 'python')
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
 if (-not (Test-CaleRunnable $mediaPython @('-V'))) { throw 'The media Python runtime did not start.' }
 Write-Output 'Installing local image, voice, vision, video and training tools...'
 & $mediaPython -m pip install --disable-pip-version-check --extra-index-url https://download.pytorch.org/whl/cu128 -r (Join-Path $Destination 'media-requirements.txt')
 if ($LASTEXITCODE -ne 0) { throw 'Media tool installation failed.' }
 try { $null=Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 }
 catch {
   Write-SetupStatus 54 'Installing and starting Ollama'
   $ollama=Join-Path $env:LOCALAPPDATA 'Programs/Ollama/ollama.exe'
   if (-not (Test-CaleRunnable $ollama)) {
     # The native Windows app handles later Ollama updates; the old CLI ZIP does not.
     $installer=Download-Verified $pins.ollama_setup.url $pins.ollama_setup.sha256 'OllamaSetup.exe'
     & $installer /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
     if ($LASTEXITCODE -ne 0) { throw 'Ollama native installation failed.' }
     if (-not (Test-CaleRunnable $ollama)) { throw 'The Ollama installer did not create a working application.' }
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
[IO.File]::WriteAllText((Join-Path $Destination '.cale-ai-install'),'Cale AI Workbench 0.5.315')
 Write-SetupStatus 100 'Your AI is ready' 'complete'
 # Optional local models continue in a separate process after the installer closes.
 Start-Process -FilePath (Join-Path $box 'python/Scripts/pythonw.exe') -ArgumentList ('"'+(Join-Path $Destination 'model_library.py')+'" --auto --status-dir "'+(Join-Path $Destination 'LongTasks')+'"') -WindowStyle Hidden
 Write-Output "Installation complete: $Destination"
} catch {
 if ($setupState) { Write-SetupStatus 0 ("Setup needs attention: " + $_.Exception.Message) 'error' }
 throw
} finally { $taskLock.ReleaseMutex(); $taskLock.Dispose() }
