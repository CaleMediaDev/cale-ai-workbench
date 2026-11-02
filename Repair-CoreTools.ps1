# Restore app-owned CLI tools when they disappear or stop responding after setup.
# Existing project files and user accounts are never touched.
[CmdletBinding()]
param([string]$Destination=$PSScriptRoot)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
. (Join-Path $PSScriptRoot 'Install-Environment.ps1')
Initialize-CalePowerShellEnvironment
$Destination=[IO.Path]::GetFullPath($Destination)
$toolbox=Join-Path $Destination 'Toolbox'
if (-not (Test-Path -LiteralPath (Join-Path $Destination '.cale-ai-install') -PathType Leaf)) {
    throw 'Cale AI Workbench has not completed its first installation.'
}
$lock=Enter-CaleInstallLock $Destination
try {
    $pins=Get-Content -LiteralPath (Join-Path $Destination 'downloads.json') -Raw | ConvertFrom-Json
    $cache=Join-Path $Destination 'Downloads'
    New-Item -ItemType Directory -Path $cache -Force | Out-Null

    function Get-VerifiedArchive([string]$url,[string]$sha,[string]$name) {
        $target=Join-Path $cache $name
        if ((Test-Path -LiteralPath $target -PathType Leaf) -and
            (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower() -eq $sha.ToLower()) {
            return $target
        }
        $partial=$target+'.partial'
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            & curl.exe --fail --location --retry 3 --output $partial $url
            if ($LASTEXITCODE -ne 0) { throw "Could not download $name" }
        } else {
            Invoke-WebRequest -Uri $url -OutFile $partial -UseBasicParsing
        }
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLower() -ne $sha.ToLower()) {
            throw "Checksum failed for $name"
        }
        Move-Item -LiteralPath $partial -Destination $target -Force
        return $target
    }

    $node=Join-Path $toolbox 'node-v26.13.0-win-x64/node.exe'
    if (-not (Test-CaleRunnable $node)) {
        $archive=Get-VerifiedArchive $pins.node.url $pins.node.sha256 'node-v26.13.0-win-x64.zip'
        Expand-Archive -LiteralPath $archive -DestinationPath $toolbox -Force
        if (-not (Test-CaleRunnable $node)) { throw 'Node.js still does not start after repair.' }
        Write-Output 'Node.js repaired.'
    }
    $github=Join-Path $toolbox 'gh_2.103.3_windows_amd64/bin/gh.exe'
    if (-not (Test-CaleRunnable $github)) {
        $archive=Get-VerifiedArchive $pins.github_cli.url $pins.github_cli.sha256 'gh_2.103.3_windows_amd64.zip'
        Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $toolbox 'gh_2.103.3_windows_amd64') -Force
        if (-not (Test-CaleRunnable $github)) { throw 'GitHub CLI still does not start after repair.' }
        Write-Output 'GitHub CLI repaired.'
    }
    $java=Join-Path $toolbox 'jdk-21.0.13+9/bin/java.exe'
    if (-not (Test-CaleRunnable $java @('-version'))) {
        $archive=Get-VerifiedArchive $pins.java.url $pins.java.sha256 'jdk.zip'
        Expand-Archive -LiteralPath $archive -DestinationPath $toolbox -Force
        if (-not (Test-CaleRunnable $java @('-version'))) { throw 'Java still does not start after repair.' }
        Write-Output 'Java repaired.'
    }
    $ghidra=Join-Path $toolbox 'ghidra_12.1.4_PUBLIC/ghidraRun.bat'
    if (-not (Test-Path -LiteralPath $ghidra -PathType Leaf)) {
        $archive=Get-VerifiedArchive $pins.ghidra.url $pins.ghidra.sha256 'ghidra-12.1.4.zip'
        Expand-Archive -LiteralPath $archive -DestinationPath $toolbox -Force
        if (-not (Test-Path -LiteralPath $ghidra -PathType Leaf)) { throw 'Ghidra is still missing after repair.' }
        Write-Output 'Ghidra repaired.'
    }
    $browserProbe=Join-Path $PSScriptRoot 'browser_probe.js'
    if (-not (Test-CaleRunnable $node @($browserProbe,$Destination,$toolbox))) {
        $env:PATH=(Split-Path $node)+';'+$env:PATH
        Copy-Item -LiteralPath (Join-Path $Destination 'package.json') -Destination $toolbox -Force
        Copy-Item -LiteralPath (Join-Path $Destination 'package-lock.json') -Destination $toolbox -Force
        Push-Location $toolbox
        try {
            & (Join-Path (Split-Path $node) 'npm.cmd') ci --ignore-scripts --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'JavaScript package repair failed.' }
            & $node (Join-Path $toolbox 'node_modules/playwright/cli.js') install chromium
            if ($LASTEXITCODE -ne 0) { throw 'Local browser download failed.' }
        } finally { Pop-Location }
        if (-not (Test-CaleRunnable $node @($browserProbe,$Destination,$toolbox))) {
            throw 'Local browser or JavaScript tools still do not work after repair.'
        }
        Write-Output 'Local browser and JavaScript tools repaired.'
    }
    Write-Output 'Managed core tools passed their checks.'
} finally {
    $lock.ReleaseMutex()
    $lock.Dispose()
}
