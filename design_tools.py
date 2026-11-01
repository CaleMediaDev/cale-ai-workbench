"""Design checks for project files; no account or online inference is needed."""

import json
import re
import subprocess
from pathlib import Path

from mcp.server import MCPServer

ROOT = Path(__file__).resolve().parent
NODE = ROOT / "Toolbox/node-v26.13.0-win-x64/node.exe"
SCANNER = ROOT / "vendor/avoid-ai-design/detect.mjs"

server = MCPServer(
    "Local design tools",
    instructions=(
        "Use scan_frontend_design on a website or app source folder, inspect the "
        "findings, then render and visually review the result. Check text colors "
        "with check_color_contrast. Scanner findings are suggestions, not proof "
        "of poor design; preserve deliberate product choices."
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
    )
    if result.returncode not in (0, 2):
        raise RuntimeError(result.stderr[-2000:] or "Design scan failed.")
    report = json.loads(result.stdout)
    report["findings"] = report.get("findings", [])[:100]
    report["note"] = "Code signals only. Render the design to check hierarchy, spacing and overall fit."
    return report


if __name__ == "__main__":
    server.run(transport="stdio")
