# Package the exact manifest-defined source, then embed it in the Windows EXE.
param([string]$Version='0.5.436')
$ErrorActionPreference='Stop'
$project=$PSScriptRoot
$out=Split-Path $project -Parent
$zip=Join-Path $out "Cale-AI-Workbench-$Version.zip"
$exe=Join-Path $out "Cale-AI-Workbench-Setup.exe"
$csc='C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw '.NET Framework C# compiler is unavailable.' }
# The one-click EXE launches PowerShell before Python exists. Catch a broken
# setup script here, before a release reaches a clean machine.
foreach ($name in @('Setup.ps1','Install-Retry.ps1','Install-Backend.ps1','Repair-CoreTools.ps1','Install-Environment.ps1','Install Long Tasks Startup.ps1')) {
    $tokens=$null; $parseErrors=$null
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $project $name),[ref]$tokens,[ref]$parseErrors) | Out-Null
    if ($parseErrors) { throw "$name has a PowerShell syntax error: $($parseErrors[0].Message)" }
}
$python='py.exe'
& $python -3.13 (Join-Path $project 'pack-release.py') $project $zip $Version
if ($LASTEXITCODE -ne 0) { throw 'Could not package release files.' }
& $csc /nologo /target:winexe /platform:x64 /out:$exe "/win32icon:$(Join-Path $project 'assets/cale-icon.ico')" "/resource:$zip,CaleAI.Bundle.zip" /r:System.Windows.Forms.dll /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll (Join-Path $project 'InstallerBootstrap.cs')
if ($LASTEXITCODE -ne 0) { throw 'Could not build the Windows installer.' }
# A beginner's ZIP has one obvious entry point. Keep the source/update archive
# separate because the updater requires its manifest paths at the ZIP root.
$installZip=Join-Path $out 'Cale-AI-Workbench-Install.zip'
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.IO.Compression
$bundleStream=[IO.File]::Open($installZip,[IO.FileMode]::Create)
$bundle=New-Object IO.Compression.ZipArchive($bundleStream,[IO.Compression.ZipArchiveMode]::Create)
try { [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($bundle,$exe,'Install Cale AI.exe') | Out-Null }
finally { $bundle.Dispose(); $bundleStream.Dispose() }
Get-FileHash -Algorithm SHA256 $zip,$exe,$installZip | Select-Object Path,Hash
