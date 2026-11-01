# Package the exact manifest-defined source, then embed it in the Windows EXE.
param([string]$Version='0.5.184')
$ErrorActionPreference='Stop'
$project=$PSScriptRoot
$out=Split-Path $project -Parent
$zip=Join-Path $out "Cale-AI-Workbench-$Version.zip"
$exe=Join-Path $out "Cale-AI-Workbench-Setup.exe"
$csc='C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw '.NET Framework C# compiler is unavailable.' }
$python='py.exe'
& $python -3.13 (Join-Path $project 'pack-release.py') $project $zip
if ($LASTEXITCODE -ne 0) { throw 'Could not package release files.' }
& $csc /nologo /target:winexe /platform:x64 /out:$exe "/win32icon:$(Join-Path $project 'assets/cale-icon.ico')" "/resource:$zip,CaleAI.Bundle.zip" /r:System.Windows.Forms.dll /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll (Join-Path $project 'InstallerBootstrap.cs')
if ($LASTEXITCODE -ne 0) { throw 'Could not build the Windows installer.' }
Get-FileHash -Algorithm SHA256 $zip,$exe | Select-Object Path,Hash
