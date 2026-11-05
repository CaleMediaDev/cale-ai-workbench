# Keep setup independent of the environment inherited from an app launcher.
# Packaged Windows apps can redirect LOCALAPPDATA writes to LocalCache\Local
# while command-line tools still see the unredirected path string.
function Initialize-CalePowerShellEnvironment {
    $standard = @(
        (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'),
        (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'),
        (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'WindowsPowerShell\Modules')
    )
    $existing = @($env:PSModulePath -split ';')
    $env:PSModulePath = (@($standard + $existing) |
        Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) } |
        Select-Object -Unique) -join ';'
}

# A half-extracted archive can leave its expected .exe behind. Run a cheap
# version command before treating any managed runtime as installed.
function Test-CaleRunnable([string]$File, [string[]]$Arguments = @('--version')) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { return $false }
    try {
        # Java prints a successful -version response on stderr. Windows
        # PowerShell 5 turns that redirected native stream into ErrorRecords;
        # under the installer's Stop preference it can look like a failed tool.
        $ErrorActionPreference='Continue'
        $null = & $File @Arguments 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

# Only the installer's own folders may be cleared when a runtime is broken.
function Reset-CaleManagedDirectory([string]$Destination, [string]$Directory) {
    $root = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
    $target = [IO.Path]::GetFullPath($Directory).TrimEnd('\')
    if (-not $target.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to reset a folder outside the Cale installation.'
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

# Read only the agent's managed-install facts. A missing or interrupted facts
# file means the bootstrap must run again before dependent tools start.
function Find-CaleHermesPython([string]$HermesHome) {
    $factsFile = Get-ChildItem (Join-Path $HermesHome 'installs/*/facts.json') -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $factsFile) { return '' }
    try {
        $facts = Get-Content -LiteralPath $factsFile.FullName -Raw | ConvertFrom-Json
        if (-not $facts.packages.venv.environment) { return '' }
        return Join-Path $facts.packages.venv.environment 'Scripts/python.exe'
    } catch { return '' }
}

# Windows reports a mutex as abandoned when the previous installer crashed.
# WaitOne grants ownership in that case, so the new run should resume setup.
function Enter-CaleInstallLock([string]$Destination) {
    $name = 'Local\CaleAIInstall-' +
        ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Destination))).Replace('/','_').Replace('\','_')
    $lock = New-Object Threading.Mutex($false, $name)
    try {
        try { $acquired = $lock.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw 'An installer is already running for this folder.' }
        return $lock
    } catch {
        $lock.Dispose()
        throw
    }
}

# On CPU-only PCs, 9B fits in RAM but can be painfully slow. Prefer the
# smaller tool-capable model until a suitable NVIDIA GPU is available.
function Select-CaleModelProfile([int]$RamGb, [double]$VramGb) {
    if ($RamGb -ge 96 -and $VramGb -ge 32) { return 'large' }
    if ($RamGb -ge 24 -and $VramGb -ge 12) { return 'compact' }
    return 'light'
}

function Get-CaleInstallDestination([string]$Requested = '') {
    $local = [IO.Path]::GetFullPath($env:LOCALAPPDATA)
    $ordinary = Join-Path $local 'CaleMedia\AIWorkbench'
    if ($Requested -and [IO.Path]::GetFullPath($Requested) -ne $ordinary) {
        return [IO.Path]::GetFullPath($Requested)
    }
    $family = $env:CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY
    if ($family -and $family -match '^[A-Za-z0-9_.-]+$') {
        if ($local -like "*\Packages\$family\LocalCache\Local") {
            return Join-Path $local 'CaleMedia\AIWorkbench'
        }
        $packageRoot = Join-Path (Join-Path $local 'Packages') $family
        if (Test-Path -LiteralPath $packageRoot -PathType Container) {
            return Join-Path $packageRoot 'LocalCache\Local\CaleMedia\AIWorkbench'
        }
    }
    return $ordinary
}

# These helpers affect only the application's runtime directories. A functioning
# Hermes runtime has its own managed facts and must not be replaced by this base.
function Get-CalePlainOwnedPath([string]$Destination,[string]$Relative) {
    $root=[IO.Path]::GetFullPath($Destination).TrimEnd('\')
    $target=[IO.Path]::GetFullPath((Join-Path $root $Relative))
    if (-not $target.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime path left the application folder.' }
    $current=$target
    while ($current -and $current.Length -ge $root.Length) {
        if (Test-Path -LiteralPath $current) {
            $item=Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Runtime paths cannot contain links or junctions.' }
        }
        if ($current -eq $root) { break }
        $current=Split-Path -Parent $current
    }
    return $target
}

function Get-CaleOwnedPythonFacts([string]$Python) {
    $probe=@'
import sys,sqlite3,_sqlite3,ctypes,json
from pathlib import Path
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.GetModuleHandleW.argtypes=[ctypes.c_wchar_p];k.GetModuleHandleW.restype=ctypes.c_void_p
k.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint32]
h=k.GetModuleHandleW('sqlite3.dll');s=ctypes.create_unicode_buffer(32768)
if not h or not k.GetModuleFileNameW(h,s,len(s)):raise RuntimeError('SQLite DLL origin unavailable')
c=sqlite3.connect(':memory:')
try:source=c.execute('select sqlite_source_id()').fetchone()[0]
finally:c.close()
print(json.dumps(dict(prefix=str(Path(sys.prefix).resolve()),executable=str(Path(sys.executable).resolve()),python='.'.join(map(str,sys.version_info[:3])),sqlite=sqlite3.sqlite_version,source=source,base=str(Path(sys.base_prefix).resolve()),stdlib=str(Path(sqlite3.__file__).resolve()),extension=str(Path(_sqlite3.__file__).resolve()),dll=str(Path(s.value).resolve()))))
'@
    $output=& $Python -I -B -c $probe
    if ($LASTEXITCODE -ne 0) { throw 'The owned Python/SQLite origin probe failed.' }
    return ($output | ConvertFrom-Json)
}

function Test-CaleOwnedPython([string]$Destination,$Pin) {
    # Compare critical bytes against the release's pins, never a self-declared
    # "healthy" marker. Then inspect the modules and DLL actually loaded.
    $ownedBase=Get-CalePlainOwnedPath $Destination ('Runtimes/'+$Pin.id+'/python')
    foreach ($entry in $Pin.critical_files.PSObject.Properties) {
        $file=Get-CalePlainOwnedPath $Destination ('Runtimes/'+$Pin.id+'/python/'+$entry.Name)
        if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or
            (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower() -ne $entry.Value) { return $false }
    }
    try {
        $facts=Get-CaleOwnedPythonFacts (Join-Path $ownedBase 'python.exe')
        return ($facts.python -eq $Pin.python -and $facts.sqlite -eq $Pin.sqlite -and $facts.source -eq $Pin.sqlite_source_id -and
            $facts.base -eq $ownedBase -and $facts.stdlib -eq (Join-Path $ownedBase 'Lib/sqlite3/__init__.py') -and
            $facts.extension -eq (Join-Path $ownedBase 'DLLs/_sqlite3.pyd') -and $facts.dll -eq (Join-Path $ownedBase 'DLLs/sqlite3.dll'))
    } catch { return $false }
}

function Test-CaleOwnedEnvironment([string]$Destination,[string]$Relative,$Pin) {
    if ($Relative -cnotin @('Toolbox/python','Media/python')) { return $false }
    if (-not (Test-CaleOwnedPython $Destination $Pin)) { return $false }
    $environment=Get-CalePlainOwnedPath $Destination $Relative
    $ownedBase=Get-CalePlainOwnedPath $Destination ('Runtimes/'+$Pin.id+'/python')
    $launchers=@{'Scripts/python.exe'='Lib/venv/scripts/nt/venvlauncher.exe';'Scripts/pythonw.exe'='Lib/venv/scripts/nt/venvwlauncher.exe'}
    foreach ($entry in $launchers.GetEnumerator()) {
        $file=Get-CalePlainOwnedPath $Destination ($Relative+'/'+$entry.Key)
        $expected=$Pin.critical_files.PSObject.Properties[$entry.Value].Value
        if (-not $expected -or -not (Test-Path -LiteralPath $file -PathType Leaf) -or
            (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower() -ne $expected) { return $false }
    }
    try {
        $python=Join-Path $environment 'Scripts/python.exe'
        $facts=Get-CaleOwnedPythonFacts $python
        return ($facts.python -eq $Pin.python -and $facts.sqlite -eq $Pin.sqlite -and $facts.source -eq $Pin.sqlite_source_id -and
            $facts.prefix -eq $environment -and $facts.executable -eq $python -and $facts.base -eq $ownedBase -and
            $facts.stdlib -eq (Join-Path $ownedBase 'Lib/sqlite3/__init__.py') -and
            $facts.extension -eq (Join-Path $ownedBase 'DLLs/_sqlite3.pyd') -and $facts.dll -eq (Join-Path $ownedBase 'DLLs/sqlite3.dll'))
    } catch { return $false }
}

function Initialize-CaleOwnedPython([string]$Destination,[string]$Archive,[string]$BootstrapPython,$Pin) {
    $relative='Runtimes/'+$Pin.id
    $target=Get-CalePlainOwnedPath $Destination $relative
    if (Test-Path -LiteralPath $target) {
        if (-not (Test-CaleOwnedPython $Destination $Pin)) { throw 'Existing owned Python failed integrity/origin checks; it was preserved for repair.' }
        return (Join-Path $target 'python/python.exe')
    }
    if ((Get-Item -LiteralPath $Archive).Length -ne $Pin.bytes -or
        (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLower() -ne $Pin.sha256) { throw 'Owned Python archive checksum failed.' }
    $stage=Get-CalePlainOwnedPath $Destination ('Runtimes/.preparing-'+[Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    $extract=@'
import sys,tarfile
from pathlib import Path,PurePosixPath
root=Path(sys.argv[2]).resolve()
with tarfile.open(sys.argv[1],'r:gz') as archive:
 members=archive.getmembers()
 if len(members)>20000 or sum(m.size for m in members)>600000000:raise ValueError('Runtime archive budget exceeded')
 seen=set()
 for m in members:
  p=PurePosixPath(m.name)
  if not p.parts or p.parts[0]!='python' or p.is_absolute() or '..' in p.parts or '\\' in m.name or ':' in m.name or not(m.isdir() or m.isfile()):raise ValueError('Unsupported runtime archive entry')
  key=m.name.casefold().rstrip('/')
  if key in seen:raise ValueError('Duplicate runtime archive entry')
  seen.add(key);(root/Path(*p.parts)).resolve().relative_to(root)
 archive.extractall(root,members=members,filter='data')
'@
    & $BootstrapPython -I -B -c $extract $Archive $stage
    if ($LASTEXITCODE -ne 0) { throw 'Owned Python extraction failed; incomplete staging was preserved.' }
    # Windows rename refuses an existing target. Never overlay an old runtime.
    [IO.Directory]::Move($stage,$target)
    if (-not (Test-CaleOwnedPython $Destination $Pin)) { throw 'Extracted owned Python failed integrity/origin checks; no environment was changed.' }
    return (Join-Path $target 'python/python.exe')
}

function Assert-CaleRuntimeCreationIdle([string]$Destination) {
    # Venv python.exe can report the global base as its process image. Its
    # command line still names the app script or venv; do not inspect exe alone.
    $root=[IO.Path]::GetFullPath($Destination).TrimEnd('\')+'\'
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        if ($process.ProcessId -eq $PID) { continue }
        if ($process.Name -match '^python(?:w)?(?:\d+(?:\.\d+)*)?\.exe$' -and
            (($process.CommandLine -and $process.CommandLine.Replace('/','\').IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0) -or
             ($process.ExecutablePath -and $process.ExecutablePath.Replace('/','\').StartsWith($root,[StringComparison]::OrdinalIgnoreCase)))) {
            throw 'Close Cale and its workers before creating or repairing Python environments.'
        }
    }
}

# Creation intent belongs to a NEW environment, not a global upgrade journal.
# Publishing it before venv runs makes a runnable but unqualified retry strict.
function Get-CaleOwnedPendingBytes([string]$Relative,$Pin) {
    if ($Relative -cnotin @('Toolbox/python','Media/python')) { throw 'Unowned Python environment.' }
    return [Text.Encoding]::UTF8.GetBytes("cale-owned-python-pending-v1`n$Relative`n$($Pin.id)`n$($Pin.sha256)`n")
}

function Test-CaleOwnedEnvironmentPending([string]$Destination,[string]$Relative,$Pin) {
    $pending=Get-CalePlainOwnedPath $Destination ($Relative+'/.cale-owned-runtime.pending')
    if (-not (Test-Path -LiteralPath $pending)) { return $false }
    if (-not (Test-Path -LiteralPath $pending -PathType Leaf)) { throw 'Unknown runtime creation intent preserved.' }
    $stream=[IO.File]::Open($pending,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try {
        $bytes=New-Object byte[] 2049;$count=0
        while ($count -lt $bytes.Length) {
            $read=$stream.Read($bytes,$count,$bytes.Length-$count)
            if ($read -eq 0) { break };$count+=$read
        }
        $expected=Get-CaleOwnedPendingBytes $Relative $Pin
        if ($count -ne $expected.Length -or [Convert]::ToBase64String($bytes,0,$count) -cne [Convert]::ToBase64String($expected)) { throw 'Unknown runtime creation intent preserved; inspect this incomplete environment.' }
    } finally { $stream.Dispose() }
    return $true
}

function Start-CaleOwnedEnvironmentCreation([string]$Destination,[string]$Relative,$Pin) {
    Assert-CaleRuntimeCreationIdle $Destination
    $null=Test-CaleOwnedEnvironmentPending $Destination $Relative $Pin
    $environment=Get-CalePlainOwnedPath $Destination $Relative
    if (Test-Path -LiteralPath $environment) {
        if (-not (Test-Path -LiteralPath $environment -PathType Container)) { throw 'Incomplete runtime is not a plain directory; existing bytes preserved.' }
        $quarantine=Get-CalePlainOwnedPath $Destination ($Relative+'.incomplete-'+[Guid]::NewGuid().ToString('N'))
        [IO.Directory]::Move($environment,$quarantine)
    }
    New-Item -ItemType Directory -Path $environment -Force | Out-Null
    $pending=Join-Path $environment '.cale-owned-runtime.pending'
    $temporary=$pending+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    $stream=[IO.File]::Open($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try {$bytes=Get-CaleOwnedPendingBytes $Relative $Pin;$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
    [IO.File]::Move($temporary,$pending) # No overwrite; no venv command before intent.
    return $environment
}

function Confirm-CaleOwnedEnvironmentCreation([string]$Destination,[string]$Relative,$Pin) {
    if (-not (Test-CaleOwnedEnvironmentPending $Destination $Relative $Pin)) { throw 'New runtime creation intent is missing; no qualification was recorded.' }
    if (-not (Test-CaleOwnedEnvironment $Destination $Relative $Pin)) { throw 'New runtime failed Python/SQLite origin and integrity checks; creation intent retained for retry.' }
    $environment=Get-CalePlainOwnedPath $Destination $Relative
    [IO.File]::Move((Join-Path $environment '.cale-owned-runtime.pending'),(Join-Path $environment '.cale-owned-runtime.qualified'))
}
