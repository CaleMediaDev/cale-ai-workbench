# Visual first-run installer. The backend owns downloads and writes progress;
# technical output stays in log files unless the person opens Details.
param([switch]$Preview,[string]$PreviewImage='')
if($PreviewImage){$Preview=$true}
. (Join-Path $PSScriptRoot 'Install-Environment.ps1')
Initialize-CalePowerShellEnvironment
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Cale AI Workbench - Install'
$iconPath=Join-Path $PSScriptRoot 'assets\cale-icon.ico'
if (Test-Path -LiteralPath $iconPath) { $form.Icon=New-Object Drawing.Icon($iconPath) }
$form.ClientSize = New-Object Drawing.Size(760,560)
$form.FormBorderStyle='FixedSingle'; $form.MaximizeBox=$false
$form.AutoScaleMode='Dpi'
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
$title.Text = 'Install Cale AI'
$title.Font = New-Object Drawing.Font('Segoe UI',19,[Drawing.FontStyle]::Bold)
$title.ForeColor=$ink
$title.SetBounds(96,23,630,40); $form.Controls.Add($title)
$info = New-Object Windows.Forms.Label
$info.Text = 'Your personal AI, ready on your PC. We will set up the tools and choose models that fit your computer.'
$info.ForeColor=$muted
$info.SetBounds(26,96,708,50); $form.Controls.Add($info)
$path = New-Object Windows.Forms.TextBox
$path.Text = Get-CaleInstallDestination
$path.ReadOnly=$true
$path.BackColor=$card; $path.ForeColor=$ink; $path.BorderStyle='FixedSingle'
$path.Visible=$false
$footer=New-Object Windows.Forms.Label
$footer.Text='Cale Media  /  calemedia.com'; $footer.ForeColor=$muted
$footer.SetBounds(26,516,708,22); $form.Controls.Add($footer)
$notice = New-Object Windows.Forms.Label
$notice.Text = 'Click Install to get started. Keep your internet connected during setup. Downloads can take a while.'
$notice.ForeColor=$muted
$notice.SetBounds(26,270,708,48); $form.Controls.Add($notice)
$stage=New-Object Windows.Forms.Label
$stage.Text='Ready to install'; $stage.Font=New-Object Drawing.Font('Segoe UI',14,[Drawing.FontStyle]::Bold)
$stage.SetBounds(26,167,650,50); $form.Controls.Add($stage)
$percent=New-Object Windows.Forms.Label
$percent.Text=''; $percent.TextAlign='MiddleRight'
$percent.SetBounds(668,167,66,32); $form.Controls.Add($percent)
$bar=New-Object Windows.Forms.ProgressBar
$bar.Minimum=0; $bar.Maximum=100; $bar.Style='Continuous'
$bar.SetBounds(26,232,708,24); $form.Controls.Add($bar)
$install = New-Object Windows.Forms.Button
$install.Text='Install'; $install.SetBounds(26,450,190,44)
$install.FlatStyle='Flat'; $install.FlatAppearance.BorderSize=0; $install.BackColor=$accent; $install.ForeColor=[Drawing.Color]::White
$form.Controls.Add($install)
$open = New-Object Windows.Forms.Button
$open.Text='Open Cale AI'; $open.Visible=$false; $open.Enabled=$false; $open.SetBounds(26,450,190,44)
$open.FlatStyle='Flat'; $open.FlatAppearance.BorderSize=0; $open.BackColor=$card; $open.ForeColor=$ink
$form.Controls.Add($open)
# Connections are visible from the first screen. A click before the Python UI
# is ready is queued; downloads keep running and the timer opens the chosen flow.
$script:pendingConnection=''
$script:connectionReady=$false
$connectionTitle=New-Object Windows.Forms.Label
$connectionTitle.Text='Connect extra AI while you wait (optional)'
$connectionTitle.Font=New-Object Drawing.Font('Segoe UI',11,[Drawing.FontStyle]::Bold)
$connectionTitle.SetBounds(26,319,708,26); $form.Controls.Add($connectionTitle)
$connectionHint=New-Object Windows.Forms.Label
$connectionHint.Text='Use your own accounts. Cale also works with local AI.'
$connectionHint.ForeColor=$muted
$connectionHint.SetBounds(26,402,708,40); $form.Controls.Add($connectionHint)
$connectionButtons=@()
$connectionProviders=@('chatgpt','claude','grok','openrouter')
$connectionLabels=@('Connect ChatGPT','Connect Claude','Connect Grok','Connect OpenRouter')
for($connectionIndex=0;$connectionIndex -lt $connectionProviders.Count;$connectionIndex++) {
    $connectionButton=New-Object Windows.Forms.Button
    $connectionButton.Text=$connectionLabels[$connectionIndex]
    $connectionButton.Tag=$connectionProviders[$connectionIndex]
    $connectionButton.SetBounds((26+182*$connectionIndex),352,162,44)
    $connectionButton.FlatStyle='Flat'; $connectionButton.FlatAppearance.BorderSize=0
    $connectionButton.BackColor=$card; $connectionButton.ForeColor=$ink
    $connectionButton.Add_Click({ Open-CaleAiConnection ([string]$this.Tag) })
    $form.Controls.Add($connectionButton); $connectionButtons+= $connectionButton
}
$log = New-Object Windows.Forms.TextBox
$log.Multiline=$true; $log.ReadOnly=$true; $log.ScrollBars='Vertical'
$log.BackColor=$card; $log.ForeColor=$ink; $log.BorderStyle='None'; $log.Font=New-Object Drawing.Font('Consolas',9)
$log.SetBounds(26,572,708,208); $log.Visible=$false; $form.Controls.Add($log)
$details=New-Object Windows.Forms.Button
$details.Text='Details'; $details.SetBounds(614,450,120,44)
$details.FlatStyle='Flat'; $details.ForeColor=$muted; $form.Controls.Add($details)
$details.Add_Click({
    $log.Visible=-not $log.Visible
    if ($log.Visible) { $form.ClientSize=New-Object Drawing.Size(760,804); $details.Text='Hide details'; Update-SetupDetails }
    else { $form.ClientSize=New-Object Drawing.Size(760,560); $details.Text='Details' }
})
$script:taskProcess=$null
$script:taskLog=Join-Path $env:TEMP ('cale-ai-install-'+[guid]::NewGuid().ToString('N')+'.log')
$script:taskError=$script:taskLog+'.errors'
$script:setupStarted=0
function Update-SetupDetails {
    $log.Text='Install folder: '+$path.Text+"`r`n`r`n"
    if(Test-Path -LiteralPath $script:taskLog){$log.AppendText((Get-Content -LiteralPath $script:taskLog -Tail 100)-join "`r`n")}
    if(Test-Path -LiteralPath $script:taskError){$log.AppendText("`r`n"+(Get-Content -LiteralPath $script:taskError -Tail 25|Out-String))}
}
$timer=New-Object Windows.Forms.Timer; $timer.Interval=1000
$timer.Add_Tick({
    if ($log.Visible) { Update-SetupDetails }
    $statusPath=Join-Path $path.Text 'LongTasks/install-progress.json'
    if(Test-Path -LiteralPath $statusPath){
        try {
            $state=Get-Content -LiteralPath $statusPath -Raw|ConvertFrom-Json
            # A prior install's 100% record must not flash Ready during retry.
            if([double]$state.updated -ge $script:setupStarted){
                $bar.Style='Continuous'; $bar.Value=[math]::Max(0,[math]::Min(100,[int]$state.percent))
                $percent.Text=[string]$bar.Value+'%'
                if($state.status -eq 'working'){$stage.Text=[string]$state.current}
                else {$stage.Text='Finishing setup and checking everything'}
                Update-CaleAiConnectionReadiness ([int]$state.percent)
            }
        } catch { } # The next timer tick retries a temporarily busy status file.
    }
    if ($script:taskProcess -and $script:taskProcess.HasExited) {
        $timer.Stop(); $install.Enabled=$true
        $bar.Style='Continuous'
        if ($script:taskProcess.ExitCode -eq 0) {
            $bar.Value=100; $percent.Text='100%'; $stage.Text='Cale AI is ready!'
            $notice.Text='Everything is set up. Open Cale AI and tell him what you want to do.'
            $install.Visible=$false; $open.Visible=$true; $open.Enabled=$true
        } else {
            $stage.Text='Setup needs another try'; $install.Text='Try again'
            $notice.Text='Your downloads are saved. Click Try again to continue. Details has the setup log if you need help.'
        }
    }
    $log.SelectionStart=$log.Text.Length; $log.ScrollToCaret()
})
$install.Add_Click({
    $install.Text='Installing...'; $install.Enabled=$false
    if($Preview){
        $stage.Text='Downloading your AI models';$percent.Text='42%';$bar.Value=42
        Update-CaleAiConnectionReadiness 42
        return
    }
    $install.Enabled=$false; $open.Enabled=$false
    $script:setupStarted=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $stage.Text='Getting your PC ready'; $percent.Text=''; $bar.Value=0; $bar.Style='Marquee'
    $notice.Text='Cale is setting things up for you. Temporary download problems are retried automatically.'
    $arguments='-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'Install-Retry.ps1')+'" -Destination "'+$path.Text+'" -Profile auto -NoLaunch'
    $script:taskProcess=Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $script:taskLog -RedirectStandardError $script:taskError
    $log.Text='Starting installation. Large model downloads can take a while. Closing this window leaves installation running.'
    $timer.Start()
})
$open.Add_Click({ Start-Process -FilePath (Join-Path $path.Text 'Toolbox/python/Scripts/pythonw.exe') -ArgumentList ('"'+(Join-Path $path.Text 'long_tasks.py')+'"') -WindowStyle Normal })
$form.AcceptButton=$install
if($PreviewImage){
    $previewTimer=New-Object Windows.Forms.Timer; $previewTimer.Interval=800
    $previewTimer.Add_Tick({
        $previewTimer.Stop()
        $bitmap=New-Object Drawing.Bitmap($form.Width,$form.Height)
        $form.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height)))
        $bitmap.Save($PreviewImage+'.progress.png'); $bitmap.Dispose()
        $form.Close()
    })
    $form.Add_Shown({
        $form.Refresh()
        $bitmap=New-Object Drawing.Bitmap($form.Width,$form.Height)
        $form.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height)))
        $bitmap.Save($PreviewImage); $bitmap.Dispose()
        $install.PerformClick(); $form.Refresh()
        $previewTimer.Start()
    })
}
[void]$form.ShowDialog()
