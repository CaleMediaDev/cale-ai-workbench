"""Run Cale's public local game fixtures; no cloud accounts or user files.

Use --runtime-root for the installed Workbench directory containing Toolbox.
This is an opt-in benchmark and writes local routing evidence in LongTasks.
It does not install models, register app tasks, or visually play-test games.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_fitness
import model_library


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument(
        "--model",
        action="append",
        required=True,
        help="Installed catalog source, e.g. gemma4:e4b; repeat to compare",
    )
    args = parser.parse_args()
    root = args.runtime_root.resolve(strict=True)
    model_library.ROOT = root
    reports = []
    for source in args.model:
        item = next(
            (
                item
                for item in model_library.CATALOG.values()
                if item.get("source") == source
            ),
            None,
        )
        if not item or not model_fitness.profile_allows(
            item, model_library.total_ram_gb(), model_fitness.gpu_vram_gb()
        ):
            parser.error("The requested catalog model does not fit this PC.")
        info = model_library.post("/api/show", {"model": source})
        if info.get("remote_host") or info.get("remote_model"):
            parser.error("Only physically installed local weights can be tested.")
        try:
            model_library.probe_tool_use(source, item.get("tool_reasoning_effort"))
            reports.append(model_fitness.probe_game_coding(source, root))
        except (OSError, ValueError, RuntimeError) as exc:
            reports.append({"source": source, "passed": False, "error": str(exc)})
    print(json.dumps(reports, indent=2))
    return (
        0
        if all(r.get("passed") and r.get("build", {}).get("passed") for r in reports)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
