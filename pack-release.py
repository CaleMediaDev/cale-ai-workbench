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
    # Developer evidence stays in source ZIPs, not the installed runtime's
    # bounded allowlist. Older updaters accept at most 250 manifest entries.
    "benchmarks/results/webapp-auto-20261103.json",
    "benchmarks/results/document-auto-20261103.json",
    "benchmarks/native_worker_smoke.py",
    "benchmarks/README.md",
    "benchmarks/game_model_compare.py",
    "benchmarks/results/game-code-probes-20261103.json",
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
