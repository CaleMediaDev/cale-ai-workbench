# Small, automatic first-run installer UI. Install-Backend.ps1 owns the actual
# downloads; this window only starts it and shows a readable progress log.
. (Join-Path $PSScriptRoot 'Install-Environment.ps1')
Initialize-CalePowerShellEnvironment
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Cale AI Workbench - Install'
$iconPath=Join-Path $PSScriptRoot 'assets\cale-icon.ico'
if (Test-Path -LiteralPath $iconPath) { $form.Icon=New-Object Drawing.Icon($iconPath) }
$form.Size = New-Object Drawing.Size(820,680)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object Drawing.Font('Segoe UI',10)
$bg=[Drawing.ColorTranslator]::FromHtml('#0B1020')
$card=[Drawing.ColorTranslator]::FromHtml('#151D31')
$ink=[Drawing.ColorTranslator]::FromHtml('#EDF3FF')
$muted=[Drawing.ColorTranslator]::FromHtml('#9AAAC6')
$accent=[Drawing.ColorTranslator]::FromHtml('#6E82FF')
$form.BackColor=$bg; $form.ForeColor=$ink
$logoPath=Join-Path $PSScriptRoot 'assets\cale-logo.png'
if (Test-Path -LiteralPath $logoPath) {
    $mark=New-Object Windows.Forms.PictureBox
    $mark.Image=[Drawing.Image]::FromFile($logoPath)
    $mark.SizeMode='Zoom'; $mark.BackColor=$bg
} else {
    $mark=New-Object Windows.Forms.Label
    $mark.Text='◆'; $mark.TextAlign='MiddleCenter'; $mark.Font=New-Object Drawing.Font('Segoe UI',22,[Drawing.FontStyle]::Bold)
    $mark.BackColor=$accent; $mark.ForeColor=[Drawing.Color]::White
}
$mark.SetBounds(24,20,60,60); $form.Controls.Add($mark)
$title = New-Object Windows.Forms.Label
$title.Text = 'CALE  /  AI WORKBENCH'
$title.Font = New-Object Drawing.Font('Segoe UI',19,[Drawing.FontStyle]::Bold)
$title.ForeColor=$ink
$title.SetBounds(96,23,690,40); $form.Controls.Add($title)
$info = New-Object Windows.Forms.Label
$info.Text = "Your AI, your PC. One install for models, tools, browser, voice, and vision. Internet is needed for downloads. By Cale Media - calemedia.com"
$info.ForeColor=$muted
$info.SetBounds(26,96,760,50); $form.Controls.Add($info)
$path = New-Object Windows.Forms.TextBox
$path.Text = Get-CaleInstallDestination
$path.ReadOnly=$true
$path.BackColor=$card; $path.ForeColor=$ink; $path.BorderStyle='FixedSingle'
$path.SetBounds(26,157,758,30); $form.Controls.Add($path)
$notice = New-Object Windows.Forms.Label
$notice.Text = 'Setup starts automatically. The workbench shows progress in My tasks. After setup, compatible large models download one at a time when RAM and storage allow.'
$notice.ForeColor=$muted
$notice.SetBounds(26,205,758,60); $form.Controls.Add($notice)
$install = New-Object Windows.Forms.Button
$install.Text='Retry setup'; $install.SetBounds(26,282,178,42)
$install.FlatStyle='Flat'; $install.FlatAppearance.BorderSize=0; $install.BackColor=$accent; $install.ForeColor=[Drawing.Color]::White
$form.Controls.Add($install)
$open = New-Object Windows.Forms.Button
$open.Text='Open app'; $open.Enabled=$false; $open.SetBounds(216,282,140,42)
$open.FlatStyle='Flat'; $open.FlatAppearance.BorderSize=0; $open.BackColor=$card; $open.ForeColor=$ink
$form.Controls.Add($open)
$log = New-Object Windows.Forms.TextBox
$log.Multiline=$true; $log.ReadOnly=$true; $log.ScrollBars='Vertical'
$log.BackColor=$card; $log.ForeColor=$ink; $log.BorderStyle='None'; $log.Font=New-Object Drawing.Font('Consolas',9)
$log.SetBounds(26,347,758,278); $form.Controls.Add($log)
$script:taskProcess=$null
$script:taskLog=Join-Path $env:TEMP ('cale-ai-install-'+[guid]::NewGuid().ToString('N')+'.log')
$script:taskError=$script:taskLog+'.errors'
$timer=New-Object Windows.Forms.Timer; $timer.Interval=1000
$timer.Add_Tick({
    if (Test-Path -LiteralPath $script:taskLog) { $log.Text=(Get-Content -LiteralPath $script:taskLog -Tail 100) -join "`r`n" }
    if ($script:taskProcess -and $script:taskProcess.HasExited) {
        $timer.Stop(); $install.Enabled=$true
        if ($script:taskProcess.ExitCode -eq 0) { $open.Enabled=$true; $log.AppendText("`r`nReady. The workbench is open. You can close this setup window.") }
        else { $log.AppendText("`r`nInstallation paused after an error. Fix the issue and click Install to retry.`r`n"+(Get-Content -LiteralPath $script:taskError -Tail 25 -ErrorAction SilentlyContinue | Out-String)) }
    }
    $log.SelectionStart=$log.Text.Length; $log.ScrollToCaret()
})
$install.Add_Click({
    $install.Enabled=$false; $open.Enabled=$false
    $arguments='-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'Install-Backend.ps1')+'" -Destination "'+$path.Text+'" -Profile auto'
    $script:taskProcess=Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $script:taskLog -RedirectStandardError $script:taskError
    $log.Text='Starting installation. Large model downloads can take a while. Closing this window leaves installation running.'
    $timer.Start()
})
$open.Add_Click({ Start-Process -FilePath (Join-Path $path.Text 'Toolbox/python/Scripts/pythonw.exe') -ArgumentList ('"'+(Join-Path $path.Text 'long_tasks.py')+'"') -WindowStyle Normal })
$form.Add_Shown({ $install.PerformClick() })
[void]$form.ShowDialog()
