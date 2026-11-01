$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot 'Toolbox/python/Scripts/pythonw.exe'
$taskScript = Join-Path $taskRoot 'long_tasks.py'
$taskArguments = '"' + $taskScript + '" --monitor'
$taskName = 'CaleAI_LongTasks'
$taskExisting = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($taskExisting -and $taskExisting.Actions.Arguments -notcontains $taskArguments) {
    throw 'A different task already uses CaleAI_LongTasks. It was not replaced.'
}
$taskUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument $taskArguments -WorkingDirectory $taskRoot
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
try {
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Principal $taskPrincipal -Description 'Resume local Hermes projects and verify their results. Uses local Ollama, no Codex model calls.' -Force | Out-Null
    'Scheduled Task installed: resumes at sign-in, retries a crashed supervisor, no runtime expiry.'
} catch {
    $taskStartup = [Environment]::GetFolderPath('Startup')
    $taskShell = New-Object -ComObject WScript.Shell
    $taskShortcut = $taskShell.CreateShortcut((Join-Path $taskStartup 'Cale AI Workbench.lnk'))
    $taskShortcut.TargetPath = $taskPython
    $taskShortcut.Arguments = $taskArguments
    $taskShortcut.WorkingDirectory = $taskRoot
    $taskShortcut.WindowStyle = 7
    $taskShortcut.Save()
    'Scheduled Task unavailable; installed current-user Startup shortcut. Resume at sign-in is enabled.'
}
$taskDesktop = [Environment]::GetFolderPath('Desktop')
$taskShell = New-Object -ComObject WScript.Shell
$taskShortcut = $taskShell.CreateShortcut((Join-Path $taskDesktop 'Cale AI Workbench.lnk'))
$taskShortcut.TargetPath = $taskPython
$taskShortcut.Arguments = '"' + $taskScript + '"'
$taskShortcut.WorkingDirectory = $taskRoot
$taskShortcut.Description = 'Persistent local AI projects, progress, pause/resume and verification'
$taskShortcut.Save()
'Desktop shortcut created: Cale AI Workbench'
# Register the task supervisor at Windows sign-in so saved work can resume.
