# Native worker reality check

`native_worker_smoke.py` sends a real tool-using request to a local Ollama model.
It creates an isolated temporary project containing a broken temperature
converter. The worker must repair it, create and run its own PowerShell verifier,
and finish its task plan. A separate grader checks four conversion cases and
puts the original bug back temporarily to confirm the verifier detects it.

Run on Windows after installing Cale and its fast model:

```powershell
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --model cale-fast
```

The script makes no cloud requests and does not register a task in the app. It
prints JSON containing elapsed time, model and tool calls, reported token use,
worker status, and independent checks. Exit code 0 means the worker reported
completion **and** the independent checks passed. Exit code 1 means either
condition failed. The task files are deleted after the run.

Use `--keep-folder C:\path\to\new-folder` to inspect the task files after a
run. `--command-timeout 10` helps diagnose a generated command that waits for
input; the default 90 seconds matches the production worker.

This is one small coding task, not a claim that Cale is better than Hermes,
OpenClaw, OpenHands, or another assistant. For a fair comparison, use the same
machine, model, starting files, acceptance checks, and time limit. Repeat runs
to account for model variation and record cold and warm model timings separately.
Expand the suite to browser, file, document, and media work before using it to
make general reliability claims.
