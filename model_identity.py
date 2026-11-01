"""Resolve local Ollama aliases from the existing installation.

Older installations use qwen3-coder-local; current fresh installs use
cale-local. The saved Hermes configuration is authoritative for which name
its requests send to the router. Source updates must not silently change it.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_MAIN = "cale-local"
DEFAULT_PERMISSIVE = "cale-permissive"
LEGACY_MAIN = "qwen3-coder-local"
LEGACY_PERMISSIVE = "qwen3-coder-permissive"
SAFE_ALIAS = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")


def configured_main_alias(root: Path) -> str:
    """Read `model.default` without importing the whole agent configuration."""
    try:
        lines = (
            (Path(root) / "data/config.yaml").read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return DEFAULT_MAIN
    in_model = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line == "model:":
            in_model = True
            continue
        if in_model and not line[0].isspace():
            break
        if in_model:
            match = re.fullmatch(r"\s+default:\s*([a-zA-Z0-9._-]+)\s*", line)
            if match:
                candidate = match.group(1).lower()
                return candidate if SAFE_ALIAS.fullmatch(candidate) else DEFAULT_MAIN
    return DEFAULT_MAIN


def configured_permissive_alias(root: Path) -> str:
    """Keep the matching legacy permissive alias on older installations."""
    return (
        LEGACY_PERMISSIVE
        if configured_main_alias(root) == LEGACY_MAIN
        else DEFAULT_PERMISSIVE
    )
