"""Installer-window artifacts are release checks, not installed app self-checks."""
import os
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(os.name=='nt','Windows installer window only')
class InstallerWindowTests(unittest.TestCase):
    def run_ps(self,body):
        # Reuse the owned PowerShell fixture without importing a second
        # collected TestCase into this module's globals.
        from Validation.test_install_environment import SymbolicLinkSetupTests
        return SymbolicLinkSetupTests.run_ps(self,body)

    def install_event(self):
        return """
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:CALE_SETUP_SOURCE,[ref]$tokens,[ref]$errors)
if($errors){throw $errors[0].Message}
$handler=$ast.Find({param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $n.Expression -is [System.Management.Automation.Language.VariableExpressionAst] -and $n.Expression.VariablePath.UserPath -eq 'install' -and $n.Member.Value -eq 'Add_Click'},$true).Arguments[0].ScriptBlock.GetScriptBlock()
$install=[pscustomobject]@{Text='Install';Enabled=$true};$open=[pscustomobject]@{Enabled=$false}
$stage=[pscustomobject]@{Text=''};$percent=[pscustomobject]@{Text=''}
$bar=[pscustomobject]@{Value=0;Style='Continuous'};$notice=[pscustomobject]@{Text=''}
$windowsTools=[pscustomobject]@{Text=''};$path=[pscustomobject]@{Text=$env:TEMP}
$form=New-Object PSObject;$form|Add-Member ScriptMethod Refresh {}
$timer=New-Object PSObject;$timer|Add-Member ScriptMethod Start {}
$log=[pscustomobject]@{Text=''}
$script:symbolicLinkSetupAttempted=$false;$script:requests=0;$script:launches=0
function Update-CaleAiConnectionReadiness {param($Percent)}
function Start-CaleSymbolicLinkSetup {$script:requests++;return @{status='ready';process=$null}}
function Start-Process {$script:launches++;return @{HasExited=$false}}
"""


    def test_actual_preview_install_event_never_requests_windows_changes(self):
        got = self.run_ps(self.install_event() + "\n$Preview=$true;& $handler\n@{requests=$script:requests;launches=$script:launches;progress=$bar.Value}|ConvertTo-Json -Compress")
        self.assertEqual(got, {'requests': 0, 'launches': 0, 'progress': 42})


    def test_actual_install_event_checks_once_and_retries_backend_without_new_uac(self):
        got = self.run_ps(self.install_event() + "\n$Preview=$false;& $handler;& $handler\n@{requests=$script:requests;launches=$script:launches;message=$windowsTools.Text}|ConvertTo-Json -Compress")
        self.assertEqual(got['requests'], 1)
        self.assertEqual(got['launches'], 2)
        self.assertEqual(got['message'], 'Windows developer tools are ready.')


