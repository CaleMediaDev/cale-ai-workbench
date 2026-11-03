# Native worker reality check

`native_worker_smoke.py` sends real tool-using requests to a local Ollama model.
Each scenario creates an isolated temporary project. The worker must finish
the request, create and run a PowerShell verifier, and close out its task plan.
Separate graders check the output and whether the verifier catches a regression:

- `temperature` repairs a Python conversion function and checks four cases.
- `contacts` cleans and deduplicates a CSV, then checks its exact rows and
  whether the verifier rejects an invalid phone number.

Run on Windows after installing Cale and its fast model:

```powershell
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --model cale-fast
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --scenario contacts --model cale-fast
```

The script makes no cloud requests and does not register a task in the app. It
prints JSON containing elapsed time, model and tool calls, reported token use,
worker status, and independent checks. Exit code 0 means the worker reported
completion **and** the independent checks passed. Exit code 1 means either
condition failed. The task files are deleted after the run.

Use `--keep-folder C:\path\to\new-folder` to inspect the task files after a
run. `--command-timeout 10` helps diagnose a generated command that waits for
input; the default 90 seconds matches the production worker.

These are two small tasks, not a claim that Cale is better than Hermes,
OpenClaw, OpenHands, or another assistant. For a fair comparison, use the same
machine, model, starting files, acceptance checks, and time limit. Repeat runs
to account for model variation and record cold and warm model timings separately.
Expand the suite to browser, document, and media work before using it to
make general reliability claims.
