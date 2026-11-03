# Native worker reality check

`native_worker_smoke.py` sends real tool-using requests to a local Ollama model.
Each scenario creates an isolated temporary project. The worker must finish
the request, create and run a PowerShell verifier, and close out its task plan.
Separate graders check the output and whether the verifier catches a regression:

- `temperature` repairs a Python conversion function and checks four cases.
- `contacts` cleans and deduplicates a CSV, then checks its exact rows and
  whether the verifier rejects an invalid phone number.
- `webapp` builds an interactive, offline HTML tip splitter. Playwright checks
  its labels, calculations, error state, and mobile width, then breaks its
  JavaScript to ensure the saved verifier rejects the regression.
- `document` reads a two-page PDF whose first page is a superseded draft.
  The grader checks final-page facts, a calculated net amount, and whether
  the saved verifier rejects a wrong total or changed source PDF.

Run on Windows after installing Cale and its fast model:

```powershell
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --model cale-fast
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --scenario contacts --model cale-fast
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --scenario webapp --model cale-fast
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --scenario document --model cale-fast
```

The script makes no cloud requests and does not register a task in the app. It
prints JSON containing elapsed time, model and tool calls, reported token use,
worker status, and independent checks. Exit code 0 means the worker reported
completion **and** the independent checks passed. Exit code 1 means either
condition failed. The task files are deleted after the run.

Use `--keep-folder C:\path\to\new-folder` to inspect the task files after a
run. `--command-timeout 10` helps diagnose a generated command that waits for
input; the default 90 seconds matches the production worker.

These are four small tasks, not a claim that Cale is better than Hermes,
OpenClaw, OpenHands, or another assistant. For a fair comparison, use the same
machine, model, starting files, acceptance checks, and time limit. Repeat runs
to account for model variation and record cold and warm model timings separately.
Expand the suite to media work and more varied documents before using it to
make general reliability claims.

The contacts scenario exercises the guarded CSV startup route. Its report
includes that model call and checks the saved result independently. Run the
temperature scenario too: it should stay on the ordinary coding path.
