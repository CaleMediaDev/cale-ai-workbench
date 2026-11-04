# Native worker reality check

## Grounding and planning experiments, v0.5.487

All six retained local runs failed overall. The brief and independent grader
were unchanged; no developer edited their app source or supplied a plan.
These are individual observations, not a controlled model/speed comparison.

| Receipt suffix | Worker seconds | Checked SQL units | Browser checks passed/attempted | Result |
| --- | ---: | ---: | ---: | --- |
| 487a | 300.02 | 1/8 | 10/15 | Guidance alone left invented HTML-text checks |
| 487b | 228.38 | 1/6 | 10/12 | Validation improved; deletion broke; empty inference response |
| 487c | 134.34 | 0/0 | 0/0 | Interaction constraint rejected; no page created |
| 487d | 300.02 | 0/4 | 8/15 | Semantic feedback recovered a plan, but its checks were invented |
| 487e | 300.02 | 0/0 | 6/9 | Default source-first; missing escapeHtml broke valid additions |
| 487f (resume) | 150.01 | 0/2 | 6/9 | Same source-first task; missing helper remained |

The first five files are `offline-expense-app-487[a-e]-20261104.json`;
the continuation is `offline-expense-resume-487f-20261104.json`. Unattempted
browser requirements are not passes. Each baseline verifier failed, so skipped
mutation tests receive no correctness credit. The source-first trial overlapped
the full CPU/browser regression run; its timing is not a controlled benchmark.
Later receipts include host-code hashes and an explicit experimental flag.

The speculative pre-implementation executable planner is now opt-in through
`--experimental-initial-plan`. Its ID/interaction schemas and guards remain
available for experiments, but better syntax did not produce a reliable app.
Default source-first work retains the SQL and browser completion requirements,
full instruction-basis checks, Stop/done controls and guarded plan insertion.
Deferred, requirement-bound phases with DOM-grounded acceptance are a proposed
next design, not an implemented or verified capability.

The browser now reports bounded unhandled JavaScript errors. Actual Chromium
tests prove missing-helper and startup errors are surfaced, and repaired/reloaded
pages clear old errors. The resumed worker above made no browser calls, so that
run does not prove model use of the new observations or successful self-repair.

The donation-credit correction was separately checked against public GitHub in
an isolated database: all 24 historical integrations were credited, within a
complete 158-fix snapshot. It does not change donated-token accounting.

## Initial planning and incremental recovery, v0.5.486

The fresh offline expense-app trial used the actual local router, without
developer edits to the app or a supplied plan. It created four SQLite units
before implementation. In 300 worker seconds it passed 10 of 15 independent
browser behaviors but completed no units. Two 150-second resumes advanced to
two, then three completed units. All three runs ended at their deadline; the
saved verifier still fails. The retained receipts are
`offline-expense-app-486-20261104.json`,
`offline-expense-resume-486a-20261104.json`, and
`offline-expense-resume-486b-20261104.json`.

Blank, zero, negative and nonnumeric input validation and phone layout remain
broken. The final unit also has an incorrect browser oracle expecting HTML
markup as visible text, although independent deletion checks pass. A failed
baseline verifier earns no mutation-testing credit. This is measurable
checkpoint progress, not a complete-app pass or a five-minute success.

## Saved form-case recovery, v0.5.485

`form_case_repair.py` isolates checker recovery on an already-correct tiny
calculator. Its saved case fills `7.2`, forgets Compute, and expects the wrong
literal text. The actual local worker must repair that case, run real browser
acceptance and the retained verifier, complete the SQLite unit, and preserve
the source bytes. The model receives the original behavior requirement, not
a supplied repair call. Run it explicitly with the installed runtime:

```powershell
python benchmarks/form_case_repair.py --runtime-root ..\Hermes-Agent --output result.json --max-seconds 60
```

The `form-case-repair-485a-20261104.json` receipt retains an overall failure:
the repair and all six independent component checks succeeded, but the default
multi-turn worker continued follow-up work and hit its inference deadline.
The subsequent `485b` receipt uses one-inference slices so its controller can
observe the component boundary. It passed in 31.22 seconds with two local
calls, a completed SQL unit, unchanged source and a passing real verifier.
The checkpoint still says `continue`; the ordinary whole-project browser gate
requires additional interaction coverage. This is an isolated repair result,
not an unassisted app build or a general speed claim.

The resumed expense-app receipts `484`, `485a` and `485b` remain failed at one
of four SQLite units. Their independent browser grader cannot find the correctly
associated Category label. The new repair API does not establish that the
model can finish this larger task. Its failed receipts remain alongside the
component result so passing a smaller probe cannot substitute for app quality.

`offline-expense-app-485-20261104.json` is also a failed fresh build, with the
original brief and no seeded source. Its 300-second inference budget expired;
no SQLite units were created despite that explicit requirement. The independent
browser checks and saved verifier failed. Mandatory initial planning and broader
task completion remain work to do; the isolated repair pass does not resolve them.

## Community report review

The [v0.5.471 review evidence](results/report-review-20261104.json) records
21 accepted contributions and one duplicate report. The complete Validation
suite passed 1,488 tests and 147 subtests, with four skipped, in 423.79 seconds.
This establishes regression coverage for the tested source snapshot; it is
not an end-to-end demonstration that the local model can build arbitrary apps.

## Failed-click repair evidence

The [expense replay](results/click-diagnostics-20261103.json) retains all 28
assertions and still reports four failures. Separate offline diagnostic replays
observe a source-backed form submit handler but no source-backed handlers on
the rebuilt Delete buttons. Source and assertion hashes remain unchanged.
Listener presence does not prove dispatch; runtime/evaluated handlers and
native actions remain possible. Evidence reaches structured step summaries and
SQLite failure memory without resetting repeated-failure counters. This is
better repair evidence, not proof that a model can now finish the app.

## Typed JavaScript logic repair

`javascript_logic_repair.py --runtime-root C:\path\to\AIWorkbench --keep-folder C:\path\to\new-folder`
asks the production local-only worker to repair four real game helper functions,
plan one SQLite unit per function, and build typed JavaScript acceptance cases.
Independent grading checks 13 behaviors and, when a saved verifier exists,
four deliberate behavior regressions. This exercises a pure module, not a
complete game, rendering, or visual quality. It does not alter user projects.

The [retained trial](results/javascript-logic-20261103.json) took 666.61 seconds,
18 validated model responses and 20 upstream local inference calls. It repaired
movement and cooldown behavior, but completed zero of four units, missed edge
collision and unspawned-wave behavior, and did not save a passing verifier.
The worker repeatedly confused positional test arguments. This is a failed
end-to-end result, not a speed or reliability claim. Failure diagnostics gained
function signatures during the run, so it is not a controlled comparison.
It does not qualify a model for complete-game routing.

The [case-recovery trial](results/javascript-logic-recovery-20261103.json) keeps
the same brief and independent grader. Its fresh run took 202.31 seconds and
24 model calls. All 13 helper behaviors passed, but only two of five planned
units completed and the saved verifier accepted two of four deliberate behavior
regressions. Its end-to-end result remains failed. Regression tests ran alongside
this trial; individual timings do not establish a speed advantage. `--resume`
continues the fixture's existing worker checkpoint and files without resetting
its code, plan, progress or SQLite units. Continuations are reported separately.

The saved checkpoint continuation took another 167.71 seconds / 24 calls and
still finished only two units. All 13 source behaviors remained correct, but
the worker edited the generated suite directly and invalidated its verifier's
checksum. The verifier then rejected the correct baseline as well as all four
mutants; those rejections do not demonstrate regression coverage. This is
another retained failure. Afterward, the worker's generic file tools were
changed to preserve generated function-suite files and direct revisions through
the checksum-bound checker API. That protection has regression-test evidence,
not another live-model success claim.

The [automatic-step report](results/automatic-steps-20261103.json) resumes that
same saved checkpoint with the automatic acceptance runner. It took 91.61
seconds / 11 local inference calls; the runner executed three structured checks
without inference and all five saved units finished. The independent grader
passed all 13 helper behaviors, the saved verifier passed correct source, and
all four deliberately broken implementations were rejected. This proves
recovery of this small module's checking/bookkeeping from an already-correct
source checkpoint. It is not a fresh build, a complete game or a controlled
speed comparison. Earlier failed attempts remain in the preceding reports.

A fresh trial from the original broken module took 539.44 seconds / 24 calls.
It planned and finished only one unit, missed the unspawned-wave behavior, and
its verifier rejected only the movement mutant. Three automatic attempts checked
that unit, but this did not provide the missing phase plan or coverage. It is
retained as a failed end-to-end build. The runner automates execution of selected
acceptance; it does not fix a model's incomplete plan or infer omitted tests.

## Further validation

The [API coverage trial](results/api-coverage-20261103.json) planned all four
requested helper functions but stopped after 428.72 seconds, 11 validated model
responses and 15 upstream calls. Its next `task_steps` arguments failed the array
schema; the rejected payload was not retained, so whether its inner list was
valid JSON is unknown. Only one unit finished, three independent behaviors
failed, and the verifier failed even on its baseline. This remains a failed
fresh build. The later live-source briefing, quoted-list compatibility and
presence-check diagnostics have separate regression evidence, not a success
claim from this run.

The later complete-game trial in the [game report](results/native-game-build-20261103.json)
took 570.34 seconds / 24 local model calls. None of its six units completed;
JavaScript syntax was invalid and mobile layout overflowed. A visible canvas
was the only passing browser observation. This is a retained failure of an
entire game, not a passing result inferred from the smaller module checks.

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

`browser_assertion_repair.py --auto-route --runtime-root C:\path\to\AIWorkbench --input-step --keep-folder C:\path\to\new-folder`
starts with a correct calculator and two prepared SQLite units. The first unit
only checks an unchanged error label after input. The worker must repair its
acceptance coverage, preserve the app bytes, finish both units and produce a
verifier that rejects both incorrect arithmetic and broken invalid-state handling.
The [input-step report](results/input-step-coverage-20261103.json) retains an initial
schema-error interruption (its inference/token counters were unavailable), and
a second run passing all independent checks in 88.21 s / 12 validated router
responses / 12 actual upstream calls. This is a verification-repair fixture,
not an app build or proof of full-game reliability.

Automatic fixture counters now distinguish validated `model_calls` from all
`router_call_attempts` and `upstream_local_calls`, including schema retries and
fallback attempts. Token totals include usage returned by rejected upstream
responses too; a timed-out response may not supply usage. Inference exceptions
produce a failed run report rather than losing already collected evidence. A
checkpoint's saved `status` does not indicate that its CLI process remains live.

`step_argument_format.py --output C:\path\to\new-report.json` compares three
seeded local Qwen calls per schema, preserving the tools and exact required
acceptance cases in the report. The [November 3 format report](results/step-argument-format-20261103.json)
records 3/3 exact data results for array-only versus 2/3 for array-or-string.
The failed old-schema sample exhausted its 1,800-token output allowance without
a usable call. Six samples do not establish general reliability or a speed gain.

`--repair-only` instead injects one synthetic quoted-array response into the
production local-only router, then sends its correction to a real local model.
No generated tool is executed. The [initial correction report](results/router-schema-repair-20261103.json)
needed an extra fallback call, despite eventually returning correct data, and
failed the strict immediate-correction fixture. After reporting expected types
without copying malformed values, the [typed-feedback report](results/router-schema-repair-typed-20261103.json)
passed in 20.21 s: one injected response and one real correction preserved every
required case. This isolates one fault-recovery path, not spontaneous error rates
or whole-app quality. The existing full worker assertion-repair fixture separately
passed in 74.06 s / 9 model calls with both SQLite units complete and its verifier
rejecting an invalid-state mutation. Both earlier failed and passing runs remain
in the [routing report](results/local-repair-routing-20261103.json).

`native_game_build.py --runtime-root C:\path\to\AIWorkbench --keep-folder C:\path\to\new-game`
asks the full native worker to build an offline space shooter, with automatic
local-only routing, real candidate approvals and at least two completed SQLite
units. Independent checks exercise the same 13 live-state game contracts as the
one-shot model fixture, real browser movement/shooting/restart, mobile width and
a saved verifier that must reject removed JavaScript. A screenshot is retained
for manual visual review. This synthetic fixture never opens or changes the
original user game. A passing fixture is not a general game quality ranking.

`--resume` continues only this fixture with the same brief and preserves the
original report; one separate `benchmark-resume-report.json` records the new
calls, initial step counts and initial worker turn count. It rejects an unrelated
project or an existing resume report rather than overwriting the evidence.

The [November 3 native game report](results/native-game-build-20261103.json)
retains two failed builds: 255.36 s / 24 calls and 230.51 s / 9 calls.
Both lacked the requested live reset function and overflowed the mobile viewport;
neither completed its database units. The first verifier also accepted removed
JavaScript. The second exposed a completion-only checker taking away build tools
while units remained unfinished; the subsequent queue-aware focus fix has a
regression test, but no passing full-game result is claimed. Exported-state
assertions separately have real offline browser, saved-verifier mutation and
SQLite step integration coverage. These are incomplete game-building results,
not evidence that Cale now produces reliable whole games.

A continuation of the second checkpoint under the step-binding changes took
another 340.90 s / 24 calls and completed one of its six units. Mobile width
passed, but the requested exported game API was missing and the verifier still
accepted removed JavaScript. This is retained as another failed end-to-end
result; partial unit progress does not qualify a game model or complete the app.

`browser_assertion_repair.py --auto-route --slices 2 --keep-folder C:\path\to\new-folder`
uses the production local-only router and native tool-failure feedback rather
than the fixed-model callback. Its report records each responding model's weight
identity, repair preference and timing, in addition to token usage and independent
grading. This variant still requires unchanged application bytes, a passing
corrected verifier, rejection of a deliberately broken app, and at least two
completed SQLite units. Routing preferences do not relax those requirements.

Use `--runtime-root C:\path\to\AIWorkbench` to read the installed runtime's
actual candidate tool-probe approvals. The [local repair routing report](results/local-repair-routing-20261103.json)
retains two runs: without candidate approvals only duplicate 9B weights were
eligible, and the task did not finish its SQLite units in 24 calls / 230.43 s.
With real installed approvals, two 9B tool failures led to one Gemma call,
then routine routing resumed. That run completed in 10 calls / 65.58 s,
preserved the HTML bytes, finished both units, passed the corrected verifier,
and rejected an invalid-state regression. Different eligibility configurations
and individual runs do not establish a general speed or reliability advantage.

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

### Reusable game workflow (November 3)

[Retained trials](results/game-starter-20261103.json) include failures and successful native builds. The final new local-only fixture finished in 67.51 seconds with seven model calls, two completed SQLite units, all 13 independent logic checks, five browser/input/layout checks, and a repeatable verifier that rejected removed JavaScript. Its short scope review failed schema validation and normal workflow completed. The preceding evolving trial took 278.77 seconds and 25 calls. This is original starter-engine reuse, not a pure-model benchmark or a controlled speed comparison. No model ranking was promoted. [Playable source](examples/local-space-shooter.html) is the actual successful synthetic fixture. Arbitrary custom game requirements remain outside this result.

### Requested gameplay values (November 3)

[Retained trials](results/game-request-contract-20261103.json) expose a real false-completion case: a five-life request was marked done while the actual game and default verifier used three. The final fresh local-only build completed in 92.55 seconds / 8 calls, passed all 13 independent logic checks and 5 browser checks, and its saved verifier rejected removed JavaScript and a forced reset to three lives. Earlier correct-artifact/incomplete-worker runs and the word-parser failure are retained. The short review rejected a paraphrased quote; ordinary work completed. [Actual five-life game](examples/five-lives-space-shooter.html) is a starter-derived configuration edit, not evidence of arbitrary game or application coding skill.


### Function-by-function repairs (November 3)

[Retained trials](results/function-phases-20261103.json) include failed runs, an
interrupted GPU-contention run, and an initially passing 13-check result that a
requirement audit invalidated: zero-area overlaps were not covered. The final
fresh explicit positive-area trial finished in 250.49 seconds / 17 model calls,
with worker completion, four completed SQL units, all 17 independent behaviors,
and a saved verifier rejecting all five deliberate regressions. Actual final
source, acceptance cases, failures, and the final audit are retained. This
proves this module-repair fixture only; arbitrary full-app construction remains
unproven. No model approval or ranking was changed.

The development router refuses new inference while a live music/image worker
holds a GPU lease in the selected runtime. Source-only benchmark checks are not
part of an installed app's validation requirements. A reasoned model-generated
case plan remains a hypothesis; compare expected results against the original
request and preserve the public input/return shapes. Never damage correct code
just to satisfy a mistaken expected value.


### Durable function notes and acceptance planning (November 3)

[Retained trials](results/function-notes-20261103.json) show both the failed
notes-only run (missed zero-area behavior) and the final balanced-planning run.
The final fresh local-only trial completed in 268.61 seconds / 18 calls,
with all 17 independent behavior checks, four completed SQL units, and a saved
verifier rejecting all five behavioral regressions. PLAN/PROGRESS match their
persisted ownership receipts; there were zero recorded model read/write/edit
operations on those notes. The reference run recorded three note writes.

The runs used evolving code and varying generated cases; this is not a general
speed comparison or model ranking. Saved unit acceptance does not establish
whole-task correctness. User-authored notes and newer project-root aliases
retain authority. Generic non-template app construction remains unproven.

### Offline expense app (November 3)

[Retained development trials](results/offline-expense-app-20261103.json) expose
an incomplete application build from a plain request. This fixture starts with
no implementation; resumes preserve its app, SQL plan, and original request.
Development interruptions added reload, dropdown, and exact button-name support
to the checker. Another run yielded automatically to a live user music worker.
The earlier exact-label grading timeout was corrected to accept punctuation and
case variants. A developer supplied an observed native-validation diagnostic;
this is not an unassisted trial or a controlled speed comparison.

The latest resumed run used 24 local model calls, passed eight observed browser
checks, then failed reload persistence. Deletion targeting timed out after the
lost rows; subsequent checks were not reached. No SQL unit was completed and
there was no passing verifier. Its actual source and reports are retained.
The independent grader requires 15 browser behaviors, completed SQL units, and
a verifier rejecting removed JavaScript and two meaningful storage faults.
Do not treat this failure, unit tests, or successful helper-function fixtures
as proof that arbitrary full-app building is reliable.

The retained `developments_v462` section continues the same assisted fixture:
12 more local calls took 416.88 seconds and completed three of five SQL units.
Twelve of 15 browser checks passed, including reload of additions; deletion,
deletion persistence, and the later decimal total failed. The baseline saved
verifier failed, so its rejection of mutations earns no correctness credit.
This process predated the final replay guard and alias-selection changes.

Inspection found 64K and 262K aliases over identical physical weights. A later
local routing probe selected the eligible 64K alias and returned `READY` in
1.07 seconds, with 7.68 GB total/GPU allocation. The observed 262K allocation
was 16.80 GB total and 9.47 GB on GPU. This confirms route/memory behavior only,
not a controlled speed or app-quality comparison. Larger eligible windows and
manual choices remain available.

The `developments_v463` section retains a later two-call resume that yielded
to a live user music worker. Its old raw `failed` outcome is qualified by the
explicit media-defer error; it is not a completed build trial. A developer
supplied a real row-delete/reload check, without editing app source, and the
owned verifier now retains those failed cases. Three SQL units and 12 of 15
browser checks remain passing; deletion is still broken. General completion
remains unproven. The runner now distinguishes `deferred` (exit 2), reports
grading separately from worker time, and skips mutations when the baseline
verifier already fails. Such a skip always leaves the overall grade failed.

A second six-call resume yielded to another live music worker after 369.48
worker seconds. Fourteen of 15 browser checks passed, but the verifier failed
and two SQL units remain unfinished. A separate unchanged-source lifecycle
probe confirms that the first deletion after reload works while the next
deletion fails after render replaces the buttons. The decimal-total check
contains that later deletion; it is not evidence of an arithmetic-only bug.
The retained suite has reached its eight-sequence bound; scalable retention
needs work without discarding earlier failures. No complete-app pass is claimed.

The `developments_v464` section retains two further failures. The first exhausted
schema correction with no valid response; actual upstream calls and tokens are
still reported. The second recorded 11 valid router responses / 14 upstream
requests in 264.71 worker seconds and regressed deletion: 12 of 15 browser
checks pass, three of six SQL units are complete, and the verifier fails.
Shape-only transport receipts identify invalid quoted step lists with trailing
extra data; no malformed JSON was silently repaired. This is not a speed gain
or reliable full-app result. Developer-supplied lifecycle checks remain retained.

The helper now retains 32 sequences / 128 cases within the same byte/wait
budgets. Tests verify fresh storage per sequence while reusing one browser
process, successful late-sequence revisions, complete hash-bound report receipts,
and rejection of stale/corrupted reports. Structured worker summaries omit the
duplicated escaped verifier transcript. The final 307 relevant regression tests
pass; the artifact above remains a real task failure needing a different repair
approach, not looser acceptance checks.
