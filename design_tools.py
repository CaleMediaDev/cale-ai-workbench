"""Design checks for project files; no account or online inference is needed."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from mcp.server import MCPServer

ROOT = Path(__file__).resolve().parent
NODE = ROOT / "Toolbox/node-v26.13.0-win-x64/node.exe"
SCANNER = ROOT / "vendor/avoid-ai-design/detect.mjs"
AI_UI_KIT = ROOT / "vendor/ai-ui"

server = MCPServer(
    "Local design tools",
    instructions=(
        "Use scan_frontend_design on a website or app source folder, inspect the "
        "findings, then render and visually review the result. Check text colors "
        "with check_color_contrast. Scanner findings are suggestions, not proof "
        "of poor design; preserve deliberate product choices. For Astro AI "
        "interfaces, add_astro_ai_ui installs a bundled, local component kit "
        "without accounts or network calls. Use it only when the project uses Astro."
    ),
)


def luminance(value: str) -> float:
    """Convert a six-digit sRGB color to relative luminance."""
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Use a six-digit hex color such as #15233A.")
    channels = [int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return sum(a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722)))


@server.tool()
def check_color_contrast(foreground: str, background: str) -> dict:
    """Measure text contrast and report WCAG AA/AAA results for normal and large text."""
    values = sorted((luminance(foreground), luminance(background)))
    ratio = (values[1] + 0.05) / (values[0] + 0.05)
    return {
        "ratio": round(ratio, 2),
        "normal_text_AA": ratio >= 4.5,
        "large_text_AA": ratio >= 3,
        "normal_text_AAA": ratio >= 7,
        "large_text_AAA": ratio >= 4.5,
    }


@server.tool()
def scan_frontend_design(path: str) -> dict:
    """Scan local HTML/CSS/JS/React for generic design patterns; return file and line findings."""
    target = Path(path).resolve()
    if not target.exists() or not (target.is_file() or target.is_dir()):
        raise ValueError("Choose an existing source file or project folder.")
    if not NODE.is_file() or not SCANNER.is_file():
        raise RuntimeError("The local design scanner is not installed yet.")
    result = subprocess.run(
        [str(NODE), str(SCANNER), "--json", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90, cwd=target if target.is_dir() else target.parent,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode not in (0, 2):
        raise RuntimeError(result.stderr[-2000:] or "Design scan failed.")
    report = json.loads(result.stdout)
    report["findings"] = report.get("findings", [])[:100]
    report["note"] = "Code signals only. Render the design to check hierarchy, spacing and overall fit."
    return report


@server.tool()
def add_astro_ai_ui(project_path: str) -> dict:
    """Add local AI chat and agent UI components to an existing Astro project.

    Files go under src/vendor/ai-ui so the project's own components are untouched.
    Repeated calls are safe; changed files at that destination are never overwritten.
    """
    project = Path(project_path).resolve()
    package_file = project / "package.json"
    if not package_file.is_file():
        raise ValueError("Choose an existing Astro project folder with package.json.")
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("The project's package.json is not valid JSON.") from exc
    dependencies = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
        **package.get("peerDependencies", {}),
    }
    if "astro" not in dependencies:
        raise ValueError("This component kit requires an Astro project.")
    source = AI_UI_KIT / "src"
    if not source.is_dir() or not (AI_UI_KIT / "LICENSE").is_file():
        raise RuntimeError("The local AI UI component kit is not installed.")
    destination = project / "src" / "vendor" / "ai-ui"
    source_files = sorted(p for p in source.rglob("*") if p.is_file())
    files = [(p, destination / p.relative_to(source)) for p in source_files]
    files.append((AI_UI_KIT / "LICENSE", destination / "LICENSE"))
    conflicts = [str(target) for original, target in files
                 if target.exists() and (not target.is_file() or target.read_bytes() != original.read_bytes())]
    if conflicts:
        raise FileExistsError("Existing AI UI files differ; no files were changed: " + ", ".join(conflicts[:5]))
    added = []
    for original, target in files:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            added.append(str(target))
    return {
        "kit": "michaelvereb/ai-ui 1.2.0 (MIT)",
        "folder": str(destination),
        "components": len(list((destination / "components" / "ai-ui").glob("*.astro"))),
        "files_added": len(added),
        "example": "In an Astro page, import Message from '../vendor/ai-ui/components/ai-ui/Message.astro'; import '../vendor/ai-ui/styles/theme.css';",
        "note": "Review component copy and styles for the project. No account, package install, or online service is required by this kit.",
    }


if __name__ == "__main__":
    server.run(transport="stdio")
