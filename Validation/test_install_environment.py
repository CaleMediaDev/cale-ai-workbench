"""Exercise the small Windows installer preflight without downloading models."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows installer only")
class InstallEnvironmentTests(unittest.TestCase):
    def test_native_stderr_does_not_make_successful_tool_look_broken(self):
        with tempfile.TemporaryDirectory() as folder:
            success = Path(folder) / "success.py"
            success.write_text("import sys\nsys.stderr.write('version\\n')\n", encoding="utf-8")
            failure = Path(folder) / "failure.py"
            failure.write_text("import sys\nsys.stderr.write('error\\n')\nsys.exit(4)\n", encoding="utf-8")
            script = Path(folder) / "probe.ps1"
            script.write_text(
                "$ErrorActionPreference='Stop'\n"
                ". $env:CALE_INSTALL_HELPER\n"
                "$yes=Test-CaleRunnable $env:CALE_TEST_PYTHON @($env:CALE_TEST_SCRIPT)\n"
                "$no=Test-CaleRunnable $env:CALE_TEST_PYTHON @($env:CALE_TEST_FAIL_SCRIPT)\n"
                "@($yes,$no) | ConvertTo-Json -Compress\n", encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(CALE_INSTALL_HELPER=str(ROOT / "Install-Environment.ps1"),
                       CALE_TEST_PYTHON=__import__("sys").executable,
                       CALE_TEST_SCRIPT=str(success), CALE_TEST_FAIL_SCRIPT=str(failure))
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [True, False])

    def test_auto_profile_needs_gpu_headroom_for_nine_billion_parameters(self):
        env = os.environ.copy()
        env["CALE_INSTALL_HELPER"] = str(ROOT / "Install-Environment.ps1")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             ". $env:CALE_INSTALL_HELPER; @((Select-CaleModelProfile 16 0),"
             "(Select-CaleModelProfile 32 0),"
             "(Select-CaleModelProfile 32 6),"
             "(Select-CaleModelProfile 32 10),"
             "(Select-CaleModelProfile 32 12),"
             "(Select-CaleModelProfile 128 0),"
             "(Select-CaleModelProfile 128 48)) | ConvertTo-Json -Compress"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         ["light", "light", "light", "light", "compact", "light", "large"])

    def test_crashed_installer_lock_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            child = base / "abandon.ps1"
            child.write_text(
                "$m=[Threading.Mutex]::OpenExisting($env:CALE_MUTEX_NAME)\n"
                "$null=$m.WaitOne()\n"
                "[Environment]::Exit(0)\n", encoding="utf-8",
            )
            harness = base / "recover.ps1"
            harness.write_text(
                ". $env:CALE_INSTALL_HELPER\n"
                "$anchor=New-Object Threading.Mutex($false,$env:CALE_MUTEX_NAME)\n"
                "try {\n"
                "  $child=Start-Process powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File \"'+$env:CALE_CHILD+'\"') -Wait -PassThru -WindowStyle Hidden\n"
                "  if ($child.ExitCode -ne 0) { throw 'child failed' }\n"
                "  $lock=Enter-CaleInstallLock $env:CALE_INSTALL\n"
                "  try { 'reacquired' } finally { $lock.ReleaseMutex(); $lock.Dispose() }\n"
                "} finally { $anchor.Dispose() }\n", encoding="utf-8",
            )
            install = str(base / "Cale")
            name = "Local\\CaleAIInstall-" + __import__("base64").b64encode(
                install.encode("utf-8")).decode("ascii").replace("/", "_").replace("\\", "_")
            env = os.environ.copy()
            env.update(CALE_INSTALL_HELPER=str(ROOT / "Install-Environment.ps1"),
                       CALE_INSTALL=install, CALE_CHILD=str(child), CALE_MUTEX_NAME=name)
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "reacquired")

    def test_interrupted_agent_facts_trigger_bootstrap_again(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            home = base / "data"
            facts = home / "installs" / "current" / "facts.json"
            facts.parent.mkdir(parents=True)
            env = os.environ.copy()
            env.update(CALE_INSTALL_HELPER=str(ROOT / "Install-Environment.ps1"),
                       CALE_HERMES_HOME=str(home))
            command = (". $env:CALE_INSTALL_HELPER; "
                       "Find-CaleHermesPython $env:CALE_HERMES_HOME")

            def probe():
                return subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-Command", command], env=env, capture_output=True, text=True, timeout=30,
                )

            self.assertEqual(probe().stdout.strip(), "")
            facts.write_text("{broken", encoding="utf-8")
            self.assertEqual(probe().stdout.strip(), "")
            facts.write_text(json.dumps({"packages": {"venv": {
                "environment": str(base / "managed-python")}}}), encoding="utf-8")
            self.assertEqual(Path(probe().stdout.strip()),
                             base / "managed-python" / "Scripts" / "python.exe")

    def test_managed_runtime_probe_and_reset_reject_outside_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            install = base / "Cale"
            broken = install / "Toolbox" / "python"
            broken.mkdir(parents=True)
            (broken / "python.exe").write_bytes(b"incomplete")
            outside = base / "user-files"
            outside.mkdir()
            harness = base / "probe.ps1"
            harness.write_text(
                ". $env:CALE_INSTALL_HELPER\n"
                "$bad=Test-CaleRunnable (Join-Path $env:CALE_BROKEN 'python.exe')\n"
                "Reset-CaleManagedDirectory $env:CALE_INSTALL $env:CALE_BROKEN\n"
                "$refused=$false\n"
                "try { Reset-CaleManagedDirectory $env:CALE_INSTALL $env:CALE_OUTSIDE } catch { $refused=$true }\n"
                "@{bad=$bad;gone=(-not (Test-Path $env:CALE_BROKEN));refused=$refused;outside=(Test-Path $env:CALE_OUTSIDE)} | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(CALE_INSTALL_HELPER=str(ROOT / "Install-Environment.ps1"),
                       CALE_INSTALL=str(install), CALE_BROKEN=str(broken),
                       CALE_OUTSIDE=str(outside))
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {"bad": False, "gone": True, "refused": True, "outside": True})

    def test_packaged_launcher_uses_physical_path_and_restores_module_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder)
            family = "Test.Codex_123"
            (local / "Packages" / family).mkdir(parents=True)
            harness = local / "probe.ps1"
            harness.write_text(
                ". $env:CALE_INSTALL_HELPER\n"
                "Initialize-CalePowerShellEnvironment\n"
                "$default=Join-Path $env:LOCALAPPDATA 'CaleMedia\\AIWorkbench'\n"
                "$resolved=Get-CaleInstallDestination $default\n"
                "$explicit=Get-CaleInstallDestination (Join-Path $env:LOCALAPPDATA 'Chosen')\n"
                "$hash=(Get-FileHash -LiteralPath $env:CALE_INSTALL_HELPER -Algorithm SHA256).Hash\n"
                "@{resolved=$resolved;explicit=$explicit;hash=$hash} | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                LOCALAPPDATA=str(local),
                CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY=family,
                PSModulePath=str(local / "missing-modules"),
                CALE_INSTALL_HELPER=str(ROOT / "Install-Environment.ps1"),
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            self.assertEqual(
                Path(actual["resolved"]),
                local / "Packages" / family / "LocalCache" / "Local" / "CaleMedia" / "AIWorkbench",
            )
            self.assertEqual(Path(actual["explicit"]), local / "Chosen")
            self.assertEqual(len(actual["hash"]), 64)

    def test_already_physical_localappdata_is_not_nested_again(self):
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "Packages" / "Test.Codex_123" / "LocalCache" / "Local"
            local.mkdir(parents=True)
            env = os.environ.copy()
            env.update(
                LOCALAPPDATA=str(local),
                CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY="Test.Codex_123",
                CALE_INSTALL_HELPER=str(ROOT / "Install-Environment.ps1"),
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 ". $env:CALE_INSTALL_HELPER; Get-CaleInstallDestination"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()), local / "CaleMedia" / "AIWorkbench")


@unittest.skipUnless(os.name == "nt", "Windows owned-runtime installer")
class OwnedFirstInstallRuntimeTests(unittest.TestCase):
    def run_ps(self, body, root, **extra):
        script = root.parent / "owned-probe.ps1"
        script.write_text("$ErrorActionPreference='Stop'\n. $env:CALE_INSTALL_HELPER\nInitialize-CalePowerShellEnvironment\n" + body, encoding="utf-8")
        env = os.environ.copy()
        env.update(CALE_INSTALL_HELPER=str(ROOT / "Install-Environment.ps1"), CALE_OWNED_ROOT=str(root), CALE_TEST_PINS=str(ROOT / "downloads.json"), **extra)
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                env=env, capture_output=True, text=True, timeout=35)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


    def test_global_python_image_with_owned_argv_blocks_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"Cale";root.mkdir()
            got=self.run_ps(r"""
function Get-CimInstance { @([pscustomobject]@{ProcessId=991;Name='pythonw.exe';ExecutablePath='C:\GlobalPython\pythonw.exe';CommandLine=('"C:\GlobalPython\pythonw.exe" "'+$env:CALE_OWNED_ROOT+'\long_tasks.py"')}) }
$blocked=$false;try { Assert-CaleRuntimeCreationIdle $env:CALE_OWNED_ROOT } catch {$blocked=$true}
@{blocked=$blocked;unchanged=(@(Get-ChildItem -LiteralPath $env:CALE_OWNED_ROOT -Force).Count -eq 0)} | ConvertTo-Json -Compress
""",root)
            self.assertEqual(got,{"blocked":True,"unchanged":True})


    def test_forward_slash_owned_argv_is_blocked_with_global_process_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"Cale";root.mkdir()
            got=self.run_ps(r"""
function Get-CimInstance { @([pscustomobject]@{ProcessId=991;Name='pythonw.exe';ExecutablePath='C:/GlobalPython/pythonw.exe';CommandLine=('"C:/GlobalPython/pythonw.exe" "'+$env:CALE_OWNED_ROOT.Replace('\','/')+'/long_tasks.py"')}) }
$blocked=$false;try {Assert-CaleRuntimeCreationIdle $env:CALE_OWNED_ROOT} catch {$blocked=$true}
@{blocked=$blocked}|ConvertTo-Json -Compress
""",root)
            self.assertEqual(got,{"blocked":True})


    def test_integrity_and_loaded_origins_are_both_required(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"Cale";home=root/"Runtimes/test-base/python";home.mkdir(parents=True)
            (home/"python.exe").write_bytes(b"fake executable")
            pin={"id":"test-base","python":"3.13.18","sqlite":"3.53.1","sqlite_source_id":"known source","critical_files":{"python.exe":hashlib.sha256(b"fake executable").hexdigest()}}
            pinpath=Path(temp)/"pin.json";pinpath.write_text(json.dumps(pin))
            got=self.run_ps(r"""
$p=Get-Content -LiteralPath $env:CALE_TEST_PIN -Raw | ConvertFrom-Json
$ownedBase=Join-Path $env:CALE_OWNED_ROOT 'Runtimes/test-base/python'
$script:wrong=$false
function Get-CaleOwnedPythonFacts { [pscustomobject]@{python='3.13.18';sqlite='3.53.1';source='known source';base=$ownedBase;stdlib=(Join-Path $ownedBase 'Lib/sqlite3/__init__.py');extension=(Join-Path $ownedBase 'DLLs/_sqlite3.pyd');dll=$(if($script:wrong){'C:\GlobalPython\sqlite3.dll'}else{Join-Path $ownedBase 'DLLs/sqlite3.dll'})} }
$valid=Test-CaleOwnedPython $env:CALE_OWNED_ROOT $p
$script:wrong=$true;$wrong=Test-CaleOwnedPython $env:CALE_OWNED_ROOT $p
$script:wrong=$false;[IO.File]::WriteAllText((Join-Path $ownedBase 'python.exe'),'tampered');$tampered=Test-CaleOwnedPython $env:CALE_OWNED_ROOT $p
@{valid=$valid;wrong=$wrong;tampered=$tampered} | ConvertTo-Json -Compress
""",root,CALE_TEST_PIN=str(pinpath))
            self.assertEqual(got,{"valid":True,"wrong":False,"tampered":False})


    def test_verified_archive_cannot_escape_owned_staging(self):
        import hashlib, io, tarfile, sys
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"Cale";root.mkdir()
            archive=Path(temp)/"malformed.tar.gz"
            with tarfile.open(archive,"w:gz") as tar:
                item=tarfile.TarInfo("python/../../outside");item.size=4;tar.addfile(item,io.BytesIO(b"evil"))
            pin={"id":"fake-base","bytes":archive.stat().st_size,"sha256":hashlib.sha256(archive.read_bytes()).hexdigest()}
            pinpath=Path(temp)/"pin.json";pinpath.write_text(json.dumps(pin))
            got=self.run_ps(r"""
$p=Get-Content -LiteralPath $env:CALE_TEST_PIN -Raw | ConvertFrom-Json
$failed=$false;try {$null=Initialize-CaleOwnedPython $env:CALE_OWNED_ROOT $env:CALE_TEST_ARCHIVE $env:CALE_TEST_BOOTSTRAP $p} catch {$failed=$true}
@{failed=$failed;published=(Test-Path -LiteralPath (Join-Path $env:CALE_OWNED_ROOT 'Runtimes/fake-base'))} | ConvertTo-Json -Compress
""",root,CALE_TEST_PIN=str(pinpath),CALE_TEST_ARCHIVE=str(archive),CALE_TEST_BOOTSTRAP=sys.executable)
            self.assertEqual(got,{"failed":True,"published":False});self.assertFalse((root/"outside").exists())


    def test_setup_pending_connection_at_38_sees_both_runtimes_on_fresh_and_partial_install(self):
        backend=(ROOT/"Install-Backend.ps1").read_text(encoding="utf-8")
        prefix=backend[backend.index("$box=Join-Path $Destination 'Toolbox'"):backend.index(" $python=Join-Path $box 'python/Scripts/python.exe'")]
        # Execute production creation orchestration and the real Setup callback.
        # Bootstrap/download/venv/probe/idle operations use TEMP fixtures; the
        # Start-Process stub checks readiness without opening a real GUI.
        prefix=prefix.replace("& $ownedPython -I -m venv", "New-TestVenv")
        setup=(ROOT/"Setup.ps1").read_text(encoding="utf-8")
        opener=setup[setup.index("function Open-CaleAiConnection("):setup.index("$connectionButtons=@()")]
        ready=setup[setup.index("                if([int]$state.percent -ge 38"):setup.index("\n            }",setup.index("                if([int]$state.percent -ge 38"))]
        for partial in [False,True]:
            with self.subTest(partial=partial),tempfile.TemporaryDirectory() as temp:
                root=Path(temp)/"Cale";root.mkdir()
                if partial:
                    scripts=root/"Toolbox/python/Scripts";scripts.mkdir(parents=True)
                    for exe in ["python.exe","pythonw.exe"]:(scripts/exe).write_bytes(b"fixture")
                body=r"""
$Destination=$env:CALE_OWNED_ROOT;$runtime='bootstrap';$LASTEXITCODE=0
$pins=[pscustomobject]@{owned_python=[pscustomobject]@{url='fixture';sha256='fixture'}}
function Write-SetupStatus {param($percent,$text)}
function Test-CaleRunnable {param($file,$arguments) return (Test-Path -LiteralPath $file)}
function Download-Verified {param($url,$sha,$name) return 'fixture-archive'}
function Initialize-CaleOwnedPython {param($destination,$archive,$bootstrap,$pin) return 'fixture-base'}
function Test-CaleOwnedEnvironment {param($destination,$relative,$pin) return $true}
function Assert-CaleRuntimeCreationIdle {param($destination)}
function New-TestVenv {param($folder) $scripts=Join-Path $folder 'Scripts';New-Item -ItemType Directory -Path $scripts -Force|Out-Null;[IO.File]::WriteAllText((Join-Path $scripts 'python.exe'),'fixture');[IO.File]::WriteAllText((Join-Path $scripts 'pythonw.exe'),'fixture')}
"""+prefix+r"""
$Preview=$false;$path=[pscustomobject]@{Text=$Destination};$connectionHint=[pscustomobject]@{Text=''};$notice=[pscustomobject]@{Text=''}
$script:connectionReady=$false;$script:pendingConnection='chatgpt';$script:launched=$false
function Start-Process {param($FilePath,$ArgumentList,$WindowStyle)
 if(-not(Test-Path -LiteralPath (Join-Path $Destination 'Toolbox/python/Scripts/python.exe')) -or -not(Test-Path -LiteralPath (Join-Path $Destination 'Media/python/Scripts/python.exe'))){throw 'Pending connection launched before both venvs existed'}
 $script:launched=$true
}
"""+opener+r"""
$state=[pscustomobject]@{percent=38}
"""+ready+r"""
@{launched=$script:launched;pending=$script:pendingConnection;ready=$script:connectionReady}|ConvertTo-Json -Compress
"""
                got=self.run_ps(body,root)
                self.assertEqual(got,{"launched":True,"pending":"","ready":True})


    def test_new_environment_requires_prefix_base_sqlite_and_copied_launcher_origins(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"Cale";env=root/"Toolbox/python";scripts=env/"Scripts";scripts.mkdir(parents=True)
            for name in ["python.exe","pythonw.exe"]:(scripts/name).write_bytes(b"fixture launcher")
            digest=hashlib.sha256(b"fixture launcher").hexdigest()
            pin={"id":"fixture-base","python":"3.13.18","sqlite":"3.53.1","sqlite_source_id":"known","critical_files":{"Lib/venv/scripts/nt/venvlauncher.exe":digest,"Lib/venv/scripts/nt/venvwlauncher.exe":digest}}
            path=Path(temp)/"pin.json";path.write_text(json.dumps(pin))
            got=self.run_ps(r"""
$p=Get-Content -LiteralPath $env:CALE_TEST_PIN -Raw|ConvertFrom-Json
$environment=Join-Path $env:CALE_OWNED_ROOT 'Toolbox/python';$base=Join-Path $env:CALE_OWNED_ROOT 'Runtimes/fixture-base/python'
function Test-CaleOwnedPython {return $true}
$script:facts=[pscustomobject]@{python='3.13.18';sqlite='3.53.1';source='known';prefix=$environment;executable=(Join-Path $environment 'Scripts/python.exe');base=$base;stdlib=(Join-Path $base 'Lib/sqlite3/__init__.py');extension=(Join-Path $base 'DLLs/_sqlite3.pyd');dll=(Join-Path $base 'DLLs/sqlite3.dll')}
function Get-CaleOwnedPythonFacts {return $script:facts}
$valid=Test-CaleOwnedEnvironment $env:CALE_OWNED_ROOT 'Toolbox/python' $p
$rejected=@()
foreach($key in @('prefix','executable','base','stdlib','extension','dll','sqlite','source')) {
 $old=$script:facts.$key;$script:facts.$key='wrong origin or version'
 $rejected+=(-not(Test-CaleOwnedEnvironment $env:CALE_OWNED_ROOT 'Toolbox/python' $p));$script:facts.$key=$old
}
[IO.File]::WriteAllText((Join-Path $environment 'Scripts/pythonw.exe'),'changed copied launcher')
$tampered=-not(Test-CaleOwnedEnvironment $env:CALE_OWNED_ROOT 'Toolbox/python' $p)
@{valid=$valid;rejected=$rejected;tampered=$tampered}|ConvertTo-Json -Compress
""",root,CALE_TEST_PIN=str(path))
            self.assertEqual(got,{"valid":True,"rejected":[True]*8,"tampered":True})

    def test_new_media_origin_failure_stops_before_dependency_or_connection_phase(self):
        source=(ROOT/"Install-Backend.ps1").read_text(encoding="utf-8")
        prefix=source[source.index("$box=Join-Path $Destination 'Toolbox'"):source.index(" $python=Join-Path $box 'python/Scripts/python.exe'")]
        prefix=prefix.replace("& $ownedPython -I -m venv","New-TestVenv")
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"Cale";root.mkdir()
            got=self.run_ps(r"""
$Destination=$env:CALE_OWNED_ROOT;$runtime='bootstrap';$LASTEXITCODE=0;$script:qualification=@()
$pins=[pscustomobject]@{owned_python=[pscustomobject]@{url='fixture';sha256='fixture'}}
function Write-SetupStatus {param($percent,$text)}
function Test-CaleRunnable {param($file,$arguments) return (Test-Path -LiteralPath $file)}
function Download-Verified {return 'archive'}
function Initialize-CaleOwnedPython {return 'base'}
function Assert-CaleRuntimeCreationIdle {}
function New-TestVenv {param($folder) New-Item -ItemType Directory -Path (Join-Path $folder 'Scripts') -Force|Out-Null;[IO.File]::WriteAllText((Join-Path $folder 'Scripts/python.exe'),'fixture')}
function Test-CaleOwnedEnvironment {param($destination,$relative,$pin) $script:qualification+=$relative;return ($relative -ne 'Media/python')}
$blocked=$false;try {
"""+prefix+r"""
} catch {$blocked=$true}
@{blocked=$blocked;qualified=$script:qualification}|ConvertTo-Json -Compress
""",root)
            self.assertEqual(got,{"blocked":True,"qualified":["Toolbox/python","Media/python"]})

    def test_healthy_existing_environments_are_not_replaced_or_requalified_as_new(self):
        source=(ROOT/"Install-Backend.ps1").read_text(encoding="utf-8")
        prefix=source[source.index("$box=Join-Path $Destination 'Toolbox'"):source.index(" $python=Join-Path $box 'python/Scripts/python.exe'")]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"Cale"
            for name in ["Toolbox/python","Media/python"]:
                p=root/name/"Scripts";p.mkdir(parents=True);(p/"python.exe").write_bytes(b"healthy old runtime")
            got=self.run_ps(r"""
$Destination=$env:CALE_OWNED_ROOT;$runtime='healthy Hermes';$pins=[pscustomobject]@{owned_python=$null}
function Write-SetupStatus {}
function Test-CaleRunnable {return $true}
function Initialize-CaleOwnedPython {throw 'must not prepare or replace existing runtime'}
function Test-CaleOwnedEnvironment {throw 'must not requalify existing as newly created'}
"""+prefix+r"""
@{unchanged=$true}|ConvertTo-Json -Compress
""",root)
            self.assertEqual(got,{"unchanged":True})
            for name in ["Toolbox/python","Media/python"]:self.assertEqual((root/name/"Scripts/python.exe").read_bytes(),b"healthy old runtime")

    def test_creation_intent_retries_failed_gate_and_real_child_crash_cutpoints(self):
        backend=(ROOT/"Install-Backend.ps1").read_text(encoding="utf-8")
        prefix=backend[backend.index("$box=Join-Path $Destination 'Toolbox'"):backend.index(" $python=Join-Path $box 'python/Scripts/python.exe'")].replace("& $ownedPython -I -m venv","New-TestVenv")
        for phase in ["before_venv","after_venv","failed_gate","after_gate","tool_qualified","dependencies"]:
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as temp:
                root=Path(temp)/"Cale";root.mkdir();script=Path(temp)/"retry.ps1"
                script.write_text(r"""$ErrorActionPreference='Stop'
. $env:CALE_INSTALL_HELPER
Initialize-CalePowerShellEnvironment
$Destination=$env:CALE_OWNED_ROOT;$runtime='healthy Hermes';$LASTEXITCODE=0
$pins=Get-Content -LiteralPath $env:CALE_TEST_PINS -Raw|ConvertFrom-Json
$script:qualified=@()
function Write-SetupStatus {}
function Test-CaleRunnable {param($file,$arguments) return(Test-Path -LiteralPath $file)}
function Download-Verified {return 'archive'}
function Initialize-CaleOwnedPython {return 'base'}
function Assert-CaleRuntimeCreationIdle {}
function New-TestVenv {param($folder)
 if(-not(Test-Path -LiteralPath (Join-Path $folder '.cale-owned-runtime.pending'))){throw 'Intent missing before venv'}
 if($folder.EndsWith('Toolbox\python') -and $env:CALE_TEST_PHASE -eq 'before_venv'){[Environment]::Exit(73)}
 if($folder.EndsWith('Media\python') -and $env:CALE_TEST_PHASE -eq 'tool_qualified'){[Environment]::Exit(73)}
 $scripts=Join-Path $folder 'Scripts';New-Item -ItemType Directory -Path $scripts -Force|Out-Null
 foreach($exe in @('python.exe','pythonw.exe')){[IO.File]::WriteAllText((Join-Path $scripts $exe),$env:CALE_TEST_PHASE)}
 if($folder.EndsWith('Toolbox\python') -and $env:CALE_TEST_PHASE -eq 'after_venv'){[Environment]::Exit(73)}
}
function Test-CaleOwnedEnvironment {param($destination,$relative,$pin)
 $script:qualified+=$relative
 if($relative -eq 'Toolbox/python' -and $env:CALE_TEST_PHASE -eq 'after_gate'){[Environment]::Exit(73)}
 return(-not($relative -eq 'Toolbox/python' -and $env:CALE_TEST_PHASE -eq 'failed_gate'))
}
$blocked=$false;try {
"""+prefix+r"""
} catch {$blocked=$true}
if($env:CALE_TEST_PHASE -eq 'dependencies'){[Environment]::Exit(73)}
@{blocked=$blocked;qualified=$script:qualified}|ConvertTo-Json -Compress
""",encoding="utf-8")
                env=os.environ.copy();env.update(CALE_INSTALL_HELPER=str(ROOT/"Install-Environment.ps1"),CALE_OWNED_ROOT=str(root),CALE_TEST_PINS=str(ROOT/"downloads.json"),CALE_TEST_PHASE=phase)
                command=["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(script)]
                first=subprocess.run(command,env=env,capture_output=True,text=True,timeout=35)
                if phase=="failed_gate":self.assertEqual(first.returncode,0,first.stderr);self.assertTrue(json.loads(first.stdout)["blocked"])
                else:self.assertEqual(first.returncode,73,first.stderr)
                env["CALE_TEST_PHASE"]="retry"
                second=subprocess.run(command,env=env,capture_output=True,text=True,timeout=35)
                self.assertEqual(second.returncode,0,second.stderr);result=json.loads(second.stdout);self.assertFalse(result["blocked"])
                expected=[] if phase=="dependencies" else ["Media/python"] if phase=="tool_qualified" else ["Toolbox/python","Media/python"]
                self.assertEqual(result["qualified"],expected)
                tool=root/"Toolbox/python"
                self.assertTrue((tool/".cale-owned-runtime.qualified").is_file());self.assertFalse((tool/".cale-owned-runtime.pending").exists())
                if phase in ["after_venv","failed_gate","after_gate"]:
                    copies=list((root/"Toolbox").glob("python.incomplete-*/Scripts/python.exe"));self.assertEqual(len(copies),1);self.assertEqual(copies[0].read_text(),phase)

    def test_unknown_pending_marker_and_partial_intent_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"Cale";environment=root/"Toolbox/python";environment.mkdir(parents=True)
            marker=environment/".cale-owned-runtime.pending";marker.write_bytes(b"unknown user bytes"*200)
            got=self.run_ps(r"""
$p=(Get-Content -LiteralPath $env:CALE_TEST_PINS -Raw|ConvertFrom-Json).owned_python
$failed=$false;try {$null=Start-CaleOwnedEnvironmentCreation $env:CALE_OWNED_ROOT 'Toolbox/python' $p} catch {$failed=$true}
@{failed=$failed}|ConvertTo-Json -Compress
""",root)
            self.assertTrue(got["failed"]);self.assertEqual(marker.read_bytes(),b"unknown user bytes"*200)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"Cale";environment=root/"Toolbox/python";environment.mkdir(parents=True)
            partial=environment/".cale-owned-runtime.pending.partial.tmp";partial.write_bytes(b"interrupted intent publication")
            got=self.run_ps(r"""
function Assert-CaleRuntimeCreationIdle {}
$p=(Get-Content -LiteralPath $env:CALE_TEST_PINS -Raw|ConvertFrom-Json).owned_python
$folder=Start-CaleOwnedEnvironmentCreation $env:CALE_OWNED_ROOT 'Toolbox/python' $p
@{pending=(Test-CaleOwnedEnvironmentPending $env:CALE_OWNED_ROOT 'Toolbox/python' $p)}|ConvertTo-Json -Compress
""",root)
            self.assertTrue(got["pending"]);copies=list((root/"Toolbox").glob("python.incomplete-*/.cale-owned-runtime.pending.partial.tmp"));self.assertEqual(len(copies),1);self.assertEqual(copies[0].read_bytes(),b"interrupted intent publication")

    def test_first_install_uses_owned_base_for_both_venvs_before_gui_launch(self):
        source=(ROOT/"Install-Backend.ps1").read_text(encoding="utf-8")
        self.assertIn("& $ownedPython -I -m venv $toolFolder",source)
        self.assertIn("& $ownedPython -I -m venv $mediaFolder",source)
        launch=source.index("Start-Process (Join-Path $box 'python/Scripts/pythonw.exe')")
        self.assertLess(source.index("& $ownedPython -I -m venv $mediaFolder"),launch)
        self.assertNotIn("uv python install '3.13'",source)




if __name__ == "__main__":
    unittest.main()
