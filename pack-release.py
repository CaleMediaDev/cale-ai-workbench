"""Create the source and installer payload from the explicit install manifest."""

import json
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
version = sys.argv[3] if len(sys.argv) > 3 else ""
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise ValueError("Pass the release version as the third argument (for example 0.5.419)")
manifest = json.loads((root / "install-files.json").read_text(encoding="utf-8"))
# The installed app runs this suite as its background self-check. A forgotten
# test would make that check look green while exercising less than development.
missing_tests = sorted(
    path.relative_to(root).as_posix()
    for path in (root / "Validation").glob("test_*.py")
    if path.relative_to(root).as_posix() not in manifest
)
if missing_tests:
    raise ValueError("Release manifest omits validation files: " + ", ".join(missing_tests))
extras = [
    "README.md", "ARCHITECTURE.md",
    "benchmarks/results/model-donations-501-20261104.json",
    "benchmarks/results/reviewed-maintenance-500-20261104.json",
    "benchmarks/results/reviewed-maintenance-499-20261104.json",
    "benchmarks/results/reviewed-maintenance-498-20261104.json",
    "benchmarks/results/pr-backlog-497-20261104.json",
    "benchmarks/results/pr-backlog-496-20261104.json",
    "benchmarks/results/pr-backlog-495-20261104.json",
    "benchmarks/results/donation-setup-hold-494-20261104.json",
    "benchmarks/results/pr-backlog-493-20261104.json",
    "benchmarks/results/offline-expense-resume-492-20261104.json",
    "benchmarks/results/verification-recovery-492-20261104.json",
    "benchmarks/results/assertion-retarget-deadend-491-20261104.json",
    "benchmarks/results/offline-expense-resume-491a-20261104.json",
    "benchmarks/results/offline-expense-resume-491b-20261104.json",
    "benchmarks/results/row-count-recovery-491-20261104.json",
    "benchmarks/results/logic-recovery-490-20261104.json",
    "benchmarks/results/offline-expense-app-489-20261104.json",
    "benchmarks/results/offline-expense-resume-490a-20261104.json",
    "benchmarks/results/pr-backlog-488-20261104.json",
    "benchmarks/results/voice-clone-probe-488-final-20261104.json",
    "benchmarks/results/pr-backlog-487-20261104.json",
    "benchmarks/results/accepted-credit-487-before-20261104.json",
    "benchmarks/results/accepted-credit-487-after-20261104.json",
    "benchmarks/results/offline-expense-app-487a-20261104.json",
    "benchmarks/results/offline-expense-app-487b-20261104.json",
    "benchmarks/results/offline-expense-app-487c-20261104.json",
    "benchmarks/results/offline-expense-app-487d-20261104.json",
    "benchmarks/results/offline-expense-app-487e-20261104.json",
    "benchmarks/results/offline-expense-resume-487f-20261104.json",
    "benchmarks/results/pr-backlog-486-20261104.json",
    "benchmarks/results/offline-expense-app-486-20261104.json",
    "benchmarks/results/offline-expense-resume-486a-20261104.json",
    "benchmarks/results/offline-expense-resume-486b-20261104.json",
    "benchmarks/form_case_repair.py",
    "benchmarks/results/form-case-repair-485a-20261104.json",
    "benchmarks/results/form-case-repair-485b-20261104.json",
    "benchmarks/results/offline-expense-resume-484-20261104.json",
    "benchmarks/results/offline-expense-resume-485a-20261104.json",
    "benchmarks/results/offline-expense-resume-485b-20261104.json",
    "benchmarks/results/offline-expense-app-485-20261104.json",
    "benchmarks/results/pr-backlog-20261104.json",
    "benchmarks/results/local-radio-20261103.json",
    "benchmarks/radio_crossfade.py",
    "benchmarks/results/radio-crossfade-20261103.json",
    "benchmarks/results/click-diagnostics-20261103.json",
    "benchmarks/results/report-review-20261104.json",
    "benchmarks/results/checker-recovery-20261104.json",
    "benchmarks/local_tool_repair.py",
    "benchmarks/results/local-tool-repair-20261104.json",
    "benchmarks/results/nested-plan-repair-20261104.json",
    "benchmarks/results/offline-expense-app-473-20261104.json",
    "benchmarks/test_inference_budget.py",
    "benchmarks/test_donation_catalog.py",
    "benchmarks/active_check_repair.py",
    "benchmarks/results/active-check-repair-481a-20261104.json",
    "benchmarks/results/active-check-repair-481b-20261104.json",
    "benchmarks/results/active-check-repair-481c-20261104.json",
    "benchmarks/results/active-check-repair-481d-20261104.json",
    "benchmarks/results/offline-expense-resume-481-20261104.json",
    "benchmarks/results/offline-expense-resume-482a-20261104.json",
    "benchmarks/results/offline-expense-resume-482b-20261104.json",
    "benchmarks/results/offline-expense-resume-483a-20261104.json",
    "benchmarks/results/offline-expense-resume-483b-20261104.json",
    "benchmarks/results/donation-review-20261104b.json",
    "benchmarks/results/offline-expense-resume-478-20261104.json",
    "benchmarks/results/offline-expense-resume-480a-20261104.json",
    "benchmarks/results/offline-expense-resume-480b-20261104.json",
    "benchmarks/results/local-server-retry-20261104.json",
    "benchmarks/results/offline-expense-app-474-20261104.json",
    "benchmarks/results/shell-checker-repair-20261104.json",
    "benchmarks/legacy_checker_recovery.py",
    "benchmarks/results/legacy-checker-recovery-20261104.json",
    "benchmarks/results/legacy-checker-recovery-resume-20261104.json",
    "benchmarks/results/legacy-checker-recovery-grade-20261104.json",
    "benchmarks/results/offline-expense-resume-476-20261104.json",
    # Developer evidence stays in source ZIPs, not the installed runtime's
    # bounded allowlist. Older updaters accept at most 250 manifest entries.
    "benchmarks/results/webapp-auto-20261103.json",
    "benchmarks/results/document-auto-20261103.json",
    "benchmarks/native_worker_smoke.py",
    "benchmarks/README.md",
    "benchmarks/game_model_compare.py",
    "benchmarks/results/game-code-probes-20261103.json",
    "benchmarks/game-review-20261103.md",
    "benchmarks/results/small-steps-20261103.json",
    "benchmarks/results/local-3d-20261103.json",
    "benchmarks/results/typed-python-checks-20261103.json",
    "benchmarks/results/webapp-steps-20261103.json",
    "benchmarks/results/numeric-coverage-20261103.json",
    "benchmarks/browser_assertion_repair.py",
    "benchmarks/results/browser-assertion-repair-20261103.json",
    "benchmarks/results/local-repair-routing-20261103.json",
    "benchmarks/native_game_build.py",
    "benchmarks/results/native-game-build-20261103.json",
    "benchmarks/step_argument_format.py",
    "benchmarks/results/step-argument-format-20261103.json",
    "benchmarks/results/router-schema-repair-20261103.json",
    "benchmarks/results/router-schema-repair-typed-20261103.json",
    "benchmarks/results/input-step-coverage-20261103.json",
    "benchmarks/javascript_logic_repair.py",
    "benchmarks/results/javascript-logic-20261103.json",
    "benchmarks/results/function-phases-20261103.json",
    "benchmarks/results/function-notes-20261103.json",
    "benchmarks/offline_expense_app.py",
    "benchmarks/results/offline-expense-app-20261103.json",
    "benchmarks/results/javascript-logic-recovery-20261103.json",
    "benchmarks/results/automatic-steps-20261103.json",
    "benchmarks/results/api-coverage-20261103.json",
    "benchmarks/results/game-starter-20261103.json",
    "benchmarks/examples/local-space-shooter.html",
    "benchmarks/examples/five-lives-space-shooter.html",
    "benchmarks/results/game-request-contract-20261103.json",
    "Install.cmd",
    "Setup.ps1",
    "Install-Retry.ps1",
    "Install-Backend.ps1",
    "InstallerBootstrap.cs",
    "build-release.ps1",
    "pack-release.py",
    "install-files.json",
]
names = sorted(set(manifest + extras))
with ZipFile(destination, "w", ZIP_DEFLATED, compresslevel=9) as archive:
    for name in names:
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"Invalid release path: {name}")
        if name == "Install-Backend.ps1":
            # Stamp the package, not the working tree. The version supplied to
            # build-release.ps1 is authoritative even when the source is older.
            backend, count = re.subn(
                r"(?m)^\$ReleaseVersion='[^'\r\n]*'",
                "$ReleaseVersion='" + version + "'",
                path.read_text(encoding="utf-8"),
            )
            if count != 1:
                raise ValueError("Installer must declare exactly one ReleaseVersion")
            archive.writestr(name, backend)
        else:
            archive.write(path, name)
# Exercise the actual updater contract before compiling/publishing an EXE.
# Source unit tests alone cannot prove that this particular ZIP is installable.
from self_update import validated_manifest

with ZipFile(destination) as archive:
    validated_manifest(archive)
print(f"Packed and update-validated {len(names)} source files in {destination}")
