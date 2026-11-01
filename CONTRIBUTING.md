# Contributing

The goal is a Windows assistant that a nontechnical person can install and understand. Use descriptive names and short comments for decisions that are not obvious from the code. Add a docstring to a new public function explaining its behavior and important side effects; avoid comments that merely repeat a line of code. Keep user-facing wording plain and test it at the minimum supported window size.

Start with [ARCHITECTURE.md](ARCHITECTURE.md) for the process and file map. Most app changes are in `long_tasks.py`; setup changes are in `Setup.ps1` and `Install-Backend.ps1`. Do not commit generated weights, installed runtimes, `Cloud/`, `LongTasks/`, user `Projects/`, logs, or account tokens. `install-files.json` is the release allowlist.

For development, install Ruff with `python -m pip install ruff`, then run `python -m ruff format .`, `python -m ruff check .`, and the relevant `Validation/test_*.py` files. The app does not require Ruff to run. Media checks use `Media/python/Scripts/python.exe` and require downloaded media weights. Parse PowerShell scripts before packaging, run `build-release.ps1`, and inspect the ZIP for the intended files and any secrets. Run the desktop app to check layout and real mouse-wheel behavior for UI changes. A passing unit test does not replace a fresh-install test when installation logic changes.

New account integrations must keep per-user credentials private, verify callback state and token identity, and make use of any shared or paid allowance an explicit user choice. Model changes must state realistic RAM and disk requirements and preserve local fallback.
