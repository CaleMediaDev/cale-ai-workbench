"""Create the source and installer payload from the explicit install manifest."""

import json
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
manifest = json.loads((root / "install-files.json").read_text(encoding="utf-8"))
extras = [
    "Install.cmd",
    "Setup.ps1",
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
        archive.write(path, name)
print(f"Packed {len(names)} source files in {destination}")
