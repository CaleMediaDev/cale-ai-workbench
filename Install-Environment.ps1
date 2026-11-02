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
