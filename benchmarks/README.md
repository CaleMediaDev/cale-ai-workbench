# Native worker reality check

## Saved browser assertion repair

`browser_assertion_repair.py --model cale-gemma4-edge --keep-folder C:\path\to\new-folder`
creates a correct app with an overstrict saved assertion: a hidden old total is
incorrectly required to have empty DOM text. The agent must repair verification,
preserve every HTML byte and the other cases, complete at least two SQLite steps,
pass its verifier, and reject an injected invalid-state regression. The task brief
states the required hidden-output behavior; recorded checker evidence locates the failing case.
All inference is local. The developer runner counts successful use of the saved-case
revision API separately from model tool-call counts.

The [November 3 report](results/browser-assertion-repair-20261103.json) includes failed
9B runs and successful/unsuccessful alternate-model runs. A 38.07-second alternate
run corrected the assertion but declared done without its requested database plan;
it remains a failed benchmark. After the first-plan completion gate was added,
another run completed three steps in 102.25 seconds, used one successful saved-case
revision, preserved the HTML and passed the regression grader. The production gate
prevents a failed first plan from silently becoming done; it does not impose a plan
on every one-step task or infer arbitrary natural-language workflow requirements.

Code and prompt details evolved during these individual runs, so they are not a
controlled speed comparison or a model ranking. Default model routing is unchanged.
The first prototype's source-preservation check also compared CRLF bytes against an
LF constant; the report retains that original result and records the byte-hash
regrade. Its overall benchmark still failed on verification and workflow. Current
runs capture the original file hash before model work. Timings include fixture seeding
and omit final independent grading, except the first prototype which included grading.

For a mistaken saved assertion, `check_webapp` accepts `saved_sequence`, `saved_case`
and `suite_sha256` from its failure, plus `cases=[one corrected assertion case]`.
The current path must match the referenced sequence. Actions remain immutable;
repeated action data is accepted only when identical. The whole retained suite
is checked after the correction. The controller does not apply suggested hidden
assertions automatically; the user's actual requirement determines the right fix.

## Named numeric examples and recovery

The [numeric coverage report](results/numeric-coverage-20261103.json) records a
fresh local 9B app run completing five steps in 304.72 seconds and passing the
stronger browser/mutation grader. It also records reopening the earlier
behaviorally correct app whose tests omitted fractional people: the new gate
identified the missing input; an initial 24-call recovery exhausted its budget,
then a 22-call continuation finished after browser argument/diagnostic fixes.
Both recovery stages left the HTML unchanged. The final verifier passed correct
behavior and rejected fractional-people, blank-bill and changed-total mutations.
The separate 148.86 and 149.96 second recovery timings exclude development pauses;
they are not an end-to-end build time or a controlled speed comparison.

The deterministic gate scans explicit invalid numeric examples only when a static
number input can be identified uniquely. Decimal-equivalent inputs count, but
numbers merely appearing in expected output do not. It skips ambiguous labels,
nonnumeric controls, range descriptions and mixed valid/invalid clauses. This
is input-presence evidence, not proof that every requested feature or assertion
is covered. Exploratory small-model reviewers produced false missing-case claims
on complete controls and were not deployed. The production gate adds no inference
call; 25 scans of one small calculator had a 3.537 ms median on the developer PC.

## Browser steps and retained coverage

`native_worker_smoke.py --scenario webapp --stepwise --model cale-fast`
builds a fresh calculator using database steps and independent offline browser
grading. The grader requires the saved verifier to reject removal of JavaScript
and targeted input-read regressions for changed totals, blank bills and fractional
people. It restores the generated source bytes after each mutation.

The [November 3 browser-step report](results/webapp-steps-20261103.json)
records three local 9B runs. The baseline exhausted 36 calls in 428.15 seconds
without a verifier. A structured-check run completed three steps in 118.65
seconds and its app passed all observed behavior checks, but the stronger grader
found that its saved tests omitted fractional people. A later prompt experiment
finished in 66.27 seconds while producing that bug and only one step. That
unproven extra wording was removed. **None of those three runs qualifies as a complete pass under
the stronger grader.** These single runs used evolving code and instructions;
they do not establish a speedup or broad app-building reliability.

Validation separately proves request-bound browser sequences survive later
checks, start with fresh state, retain independent assertions for identical
inputs, detect earlier-page regressions, and clear only actually verified page
diagnostics. The small model still needs better requirement-coverage reasoning;
storage and replay cannot supply cases it never writes.

## Structured Python acceptance checks

Run `native_worker_smoke.py --scenario shipping --stepwise --model cale-fast`
to exercise two pricing zones, fractional weights, defaults and invalid inputs.
The independent grader also replaces the correct function with four separate
pricing/validation defects and requires the model's saved verifier to reject each.
It restores the original bytes afterward. These are isolated synthetic projects.

The [November 3 typed-check report](results/typed-python-checks-20261103.json)
records a local 9B temperature repair completing in 72.65 seconds with 15 calls,
and a fresh shipping repair in 203.05 seconds with 34 calls. Neither supplied a
verifier. Shipping used structured SQLite acceptance steps, passed all 11
independent behavior cases, and caught all four mutations. An earlier shipping
checkpoint had correct implementation but retained only invalid-input cases;
the stronger pricing mutations expose that insufficient verifier. The new run
also initially chose a wrong expected price and wasted calls editing generated
JSON instead of revising its SQLite step. Current failure messages identify that
distinction and give the exact revision action. The observed timings predate
that final message improvement; no speed benefit is claimed for it.

Cases still come from the model, so the helper cannot prove requirement coverage.
Broader apps need integration tests. Different runs used evolving code, not a
controlled speed comparison. The explicit database migration, fresh-source
checks, cross-file case retention, invalid data and revised-step recovery are
also covered by the validation suite.

## Local 3D integration checks

The [November 3 3D report](results/local-3d-20261103.json) records actual local
exports on an RTX 3060 12 GB / 128 GB RAM PC. TripoSR generated and reloaded a
20,420-triangle chair GLB in 17 seconds with its runtime and weights installed;
first setup plus generation took 167 seconds. A text prompt through the local
image maker and TripoSR took 47.79 seconds. CPU primitive geometry exported to
GLB, OBJ and STL. Software previews were rendered and visually inspected.

TripoSR was selected for its tested Windows compatibility, small-GPU fit and
MIT-licensed local code/weights. This is not a global quality ranking. The
official TRELLIS.2 requirements call for Linux and at least 24 GB GPU memory;
that backend is not installed or claimed to work by this release. Neural assets
are rough vertex-colored drafts; validity checks do not prove faithful shape,
production topology, printable solids, textures or rigging.

## Native worker fixtures

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
Toolbox\python\Scripts\python.exe benchmarks\native_worker_smoke.py --scenario temperature --stepwise --prepared-checks --model cale-fast
```

The script makes no cloud requests and does not register a task in the app. It
prints JSON containing elapsed time, model and tool calls, reported token use,
worker status, and independent checks. Exit code 0 means the worker reported
completion **and** the independent checks passed. Exit code 1 means either
condition failed. The task files are deleted after the run.

`--stepwise` additionally requires at least two completed SQLite units. For the
temperature fixture, `--prepared-checks` supplies a known read-only verifier
before model execution. That variant isolates repairing and advancing steps
from authoring a verifier; it does not prove that the model writes good tests.
The grader requires a verifier to pass the correct code and reject the old bug.

The [November 3 small-step report](results/small-steps-20261103.json) records
a local 9B run finishing two SQLite units in 50.85 seconds and 13 model calls.
The supplied verifier was preserved, passed the repaired function, and rejected
the original bug. An earlier run without supplied checks fixed the function
but got stuck authoring its verifier. This supports the tested step orchestration,
not a claim that small models can reliably write tests or complete entire apps.

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
