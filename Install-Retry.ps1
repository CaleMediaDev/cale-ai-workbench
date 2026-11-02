# Run first-time setup independently of the progress window. A person can close
# the window while this process keeps repairing transient download/tool errors.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$Destination,
 [ValidateSet('auto','large','compact','light')][string]$Profile='auto',
 [ValidateRange(1,5)][int]$MaxAttempts=3,
 [ValidateRange(0,300)][int]$RetryDelaySeconds=15
)
$ErrorActionPreference='Stop'
$backend=Join-Path $PSScriptRoot 'Install-Backend.ps1'
if (-not (Test-Path -LiteralPath $backend)) { throw 'The setup program is incomplete: Install-Backend.ps1 is missing.' }

for ($attempt=1; $attempt -le $MaxAttempts; $attempt++) {
 Write-Output "Setup attempt $attempt of $MaxAttempts. Existing downloads and installed tools will be reused."
 $recent=New-Object 'System.Collections.Generic.List[string]'
 # Windows PowerShell turns redirected native stderr into ErrorRecords. Keep
 # streaming those lines rather than terminating the retry runner itself.
 $ErrorActionPreference='Continue'
 $attemptStarted=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
 & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $backend -Destination $Destination -Profile $Profile 2>&1 | ForEach-Object {
   $line=[string]$_
   Write-Output $line
   $recent.Add($line)
   if ($recent.Count -gt 30) { $recent.RemoveAt(0) }
 }
 $code=$LASTEXITCODE
 $ErrorActionPreference='Stop'
 if ($code -eq 0) { Write-Output 'Setup finished successfully.'; exit 0 }
 $lastError=$recent -join "`n"
 # The backend records its actual failure beside the installation. Prefer
 # that structured result to native stderr, whose formatting can vary under
 # Windows PowerShell and may lose the decisive line in a long error trace.
 $statusPath=Join-Path $Destination 'LongTasks/install-progress.json'
 if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
   try {
     $status=Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
     if ($status.status -eq 'error' -and
         [double]$status.installer_started -ge ($attemptStarted-1)) {
       $lastError=[string]$status.current+"`n"+$lastError
     }
   } catch { }
 }
 # These errors cannot be fixed by repeating the same install automatically.
 if ($lastError -match '(?i)requires 64-bit x86 Windows|needs about \d+ GB RAM|not enough free disk space|choose an empty folder|checksum failed') {
   Write-Error "Setup stopped because the PC or download needs attention (exit $code)."
   exit $code
 }
 if ($attempt -ge $MaxAttempts) {
   Write-Error "Setup did not finish after $MaxAttempts attempts (last exit $code)."
   exit $code
 }
 $delay=$RetryDelaySeconds*$attempt
 Write-Output "Setup hit an error. Retrying automatically in $delay seconds."
 if ($delay -gt 0) { Start-Sleep -Seconds $delay }
}
