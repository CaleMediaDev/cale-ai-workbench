# Native worker reality check

`game_model_compare.py --runtime-root C:\path\to\AIWorkbench --model gemma4:e4b --model qwen3.5:9b`
runs the local tool-call check, two sets of 13 pure game-logic cases, and a complete
HTML game generation with 13 independent live-state/logic checks. The generated
game must expose a specified API using its actual gameplay state. The lightweight
DOM/canvas stubs do not prove visual design, browser input, responsive layout or
that the model makes better games generally. These checks run without a browser
or cloud request. Only use installed catalog models that fit the PC.

The [November 3 game probe report](results/game-code-probes-20261103.json) records
both models passing the small functions and **both failing the larger game
fixture**. Gemma 4 E4B was faster, but that failure prevents automatic promotion
as a game builder. Qwen 2.5 Coder 7B and 14B advertised tools but failed the real
multiply call on the developer's installation; neither was approved. Qwen 3.8
27B was hardware-excluded on the RTX 3060 12 GB, so no speed claim is made for it.
These are small, single-machine probes, not universal model rankings.
The subsequent Qwen 3.5 35B benchmark ended with a timeout and no complete
result. It cannot be ranked as a passing winner. Future failed runs report
the elapsed wall time and whether tool qualification or game generation failed.

The [original space shooter review](game-review-20261103.md) records the
frame-rate, wave progression, focus, layout and verification issues found in
the completed project. No hands-on visual play-test claim is made.

`native_worker_smoke.py` sends real tool-using requests to a local Ollama model.
Each scenario creates an isolated temporary project. The worker must finish
the request, create and run a PowerShell verifier, and close out its task plan.
Separate graders check the output and whether the verifier catches a regression:

- `temperature` repairs a Python conversion function and checks four cases.
- `contacts` cleans and deduplicates a CSV, then checks its exact rows and
  whether the verifier rejects an invalid phone number.
- `webapp` builds an interactive, offline HTML tip splitter. Playwright checks
  its number input types, calculations, invalid and fractional people counts,
  blank bill state, and mobile width, then breaks its
  JavaScript to ensure the saved verifier rejects the regression.
- `document` reads a two-page PDF whose first page is a superseded draft.
  The grader checks final-page facts, a calculated net amount, and whether
  the saved verifier rejects a wrong total or changed source PDF.
- `webapp_repair` starts with an existing tip splitter that truncates fractional
  people counts. Before the worker starts, a real offline browser failure is
  recorded in SQLite. The fresh worker must recover that diagnostic, repair
  the existing page, and pass the independent webapp grader and mutation check.
  No failed browser message is planted in its recent conversation history.

Run on Windows after installing Cale and its fast model:

```powershell
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --model cale-fast
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --scenario contacts --model cale-fast
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --scenario webapp --model cale-fast
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --scenario document --model cale-fast
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --scenario webapp_repair --model cale-fast
```

The script makes no cloud requests and does not register a task in the app. It
prints JSON containing elapsed time, model and tool calls, reported token use,
worker status, and independent checks. Exit code 0 means the worker reported
completion **and** the independent checks passed. Exit code 1 means either
condition failed. The task files are deleted after the run.

Use `--keep-folder C:\path\to\new-folder` to inspect the task files after a
run. `--command-timeout 10` helps diagnose a generated command that waits for
input; the default 90 seconds matches the production worker.

These are five small tasks, not a claim that Cale is better than Hermes,
OpenClaw, OpenHands, or another assistant. For a fair comparison, use the same
machine, model, starting files, acceptance checks, and time limit. Repeat runs
to account for model variation and record cold and warm model timings separately.
Expand the suite to media work and more varied documents before using it to
make general reliability claims.

On November 3, 2026, a fresh webapp run with the local `cale-main-64k`
model completed in 333.4 seconds, using nine model calls and twelve tool
calls. Every independent check passed, including fractional people, blank
bill, mobile width, and rejection of deliberately broken JavaScript. This
followed a worker instruction change requiring separate cases for every
explicit example and boundary in the request. The earlier run had omitted
the fractional case and failed the independent grader. This single run used
a fixed model; it does not establish repeatability or automatic routing quality.

The contacts scenario exercises the guarded CSV startup route. Its report
includes that model call and checks the saved result independently. Run the
temperature scenario too: it should stay on the ordinary coding path.

## Recorded automatic local runs

These isolated synthetic tasks ran on the developer's Windows PC with cloud
access disabled. The reports record actual router choices and independent
grades. They are single runs with different app versions, not a controlled
speed comparison or a general reliability score.

| Workflow | App version | Time | Model calls | Independent result |
| --- | --- | --- | --- | --- |
| [Offline tip splitter](results/webapp-auto-20261103.json) | 0.5.417 | 467.03 s | 8 | Every check passed, including invalid fractions and deliberately broken JavaScript |
| [Two-page document summary](results/document-auto-20261103.json) | 0.5.424 | 106.87 s | 7 | Final terms and arithmetic correct; verifier rejected a wrong total and changed source |

The document fixture is generated by the benchmark: page 1 is a superseded
draft and page 2 contains the final terms. These results contain no user
documents, account credentials, or cloud inference usage.

During development of 0.5.434, an automatic local repair completed the original
tip-splitter examples in 144.53 seconds with 12 model calls. Inspection then
found a superficial fix that rejected `4.0` as fractional. After extending both
the request and grader to include numerically integral decimal and exponent
inputs, another run took 468.86 seconds and exhausted its 24-call budget. Its
page passed the numerical cases, but the worker did not finish its verifier.
This remains an end-to-end reliability failure, not a successful benchmark.
These runs used evolving worker code, so they are not controlled comparisons.
