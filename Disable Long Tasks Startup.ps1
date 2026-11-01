$taskName = 'CaleAI_LongTasks'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task -and $task.Actions.Arguments -match [regex]::Escape((Join-Path $PSScriptRoot 'long_tasks.py'))) {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
}
$taskShortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'Cale AI Workbench.lnk'
if (Test-Path -LiteralPath $taskShortcut) {
    $taskShell = New-Object -ComObject WScript.Shell
    $taskTarget = $taskShell.CreateShortcut($taskShortcut)
    if ($taskTarget.Arguments -match [regex]::Escape((Join-Path $PSScriptRoot 'long_tasks.py'))) {
        Remove-Item -LiteralPath $taskShortcut
    }
}
'Automatic startup disabled. Pause projects in Cale AI Workbench to stop current work.'
