# Cale AI Workbench

**A Windows assistant that builds with tools, saves its progress, and runs local models.**

Release 0.5.537 includes 24 reviewed donation fixes for task progress, routing, browser observations, local model predictions, setup and media recovery. Four proposals were corrected before acceptance; the FTS regression was rejected. A test-only self-check correction removes a fragile startup-time assumption. [Validation and limits](benchmarks/results/reviewed-donations-537-20261106.json).

Release 0.5.536 includes 14 reviewed donation fixes for local memory, file handling, routing, voice setup, image recovery, music tools and gauges. One unsafe proposal was rejected; decoder compatibility coverage is explicit. [Validation and limits](benchmarks/results/reviewed-donations-536-20261106.json).

Release 0.5.535 adds browser-computed accessible role/name checks, preserves them during saved-check repairs, and keeps the tool catalog within the existing small-context regression budgets. [Validation and limits](benchmarks/results/accessibility-checker-535-20261105.json).

Release 0.5.534 reduces self-check progress database overhead by reusing its owned writer connection. Each update still commits separately, and completion gates remain intact. [Validation and limits](benchmarks/results/selfcheck-writer-534-20261105.json).

Release 0.5.533 shows the exact saved PR link when GitHub cannot publicly confirm a donation, labelled unconfirmed. Saved work and duplicate-submission protection remain intact. [Validation and limits](benchmarks/results/pending-proposal-link-533-20261105.json).

Release 0.5.532 integrates reviewed donated fixes for task timing, local memory, file handling, voice workers, music processing and recovery. Contributions were reproduced, corrected where needed and checked together. [Validation and limits](benchmarks/results/reviewed-donations-532-20261105.json).

Release 0.5.531 includes three reviewed donated fixes: recovery from malformed self-check reports, invalid game-model probe responses, and tool-call metadata in social speech. [Validation and limits](benchmarks/results/donated-recovery-531-20261105.json).

Release 0.5.530 preserves reported Claude subscription usage when a bounded coding step ends in an error, rejects unrecognized completion envelopes, and handles malformed authentication status cleanly. [Validation and limits](benchmarks/results/claude-result-accounting-530-20261105.json).

Release 0.5.529 gives scheduled trained-model donations a durable upload journal, verified fork reuse and public delivery checks. Qualified cached uploads retry after one minute without retraining; the original hourly training limit and trainer bytes remain unchanged. SQLite creates the new table automatically. [Validation and limits](benchmarks/results/model-upload-journal-529-20261105.json).

Release 0.5.528 confirms durable code proposals through the public GitHub API before announcing delivery. Hidden or unavailable results keep their saved work and retry without more AI generation or duplicate PRs. Existing cached proposal policy stays compatible. This does not bypass GitHub restrictions or cover the separate model-experiment publisher. [Validation and limits](benchmarks/results/public-code-delivery-528-20261105.json).

Release 0.5.527 reuses verified contribution forks and puts exact GitHub failures in the main chat. Saved drafts recover without generating the same proposal again. GitHub permission refusals still cannot be overridden. [Validation and limits](benchmarks/results/donation-fork-recovery-527-20261105.json).

Release 0.5.526 corrects a misleading donation chat update: one waiting attempt does not mean every source or GitHub is disconnected. Open Donate AI power for current status. [Validation and limits](benchmarks/results/donation-wait-chat-526-20261105.json).

Release 0.5.525 fixes automatic updates for connected accounts and makes recovered task context explicit about saved progress and unavailable database facts. It retains the 0.5.524 proposal-search fix. Connected users stuck on 0.5.523/524 can run the Windows installer once to receive the update-guard correction. [Validation and limits](benchmarks/results/runtime-context-updater-525-20261105.json).

Release 0.5.524 removes GitHub author-search dependency from donation discovery, fixing the reproduced HTTP422 proposal-search failure. Complete pull listings retain duplicate-work checks and saved draft accounting. Chrome manifest recognition and the camera self-check fixture are also corrected. [Validation and limits](benchmarks/results/proposal-discovery-524-20261105.json).

Release 0.5.523 saves finished donation drafts across connection failures and restarts, avoids duplicate submissions after uncertain responses, and keeps token counters responsive. The donation panel can stay open during automatic updates once account connections finish. [Validation and limits](benchmarks/results/durable-donation-523-20261105.json).

Release 0.5.522 explains donation failures by GitHub step/status or local worker category, shares GitHub cooldowns across lanes, and fixes installed self-check fixture dependencies. The installer and installed checks share the actual connection callbacks. [Validation and limits](benchmarks/results/reliability-donation-522-20261105.json).

Release 0.5.521 preserves a live self-check through brief database contention, keeps incomplete runs from passing, and fixes valid Chrome manifest directives. Scoped checks passed 59 self-check tests and 28 Chrome tests, plus four independent pause/source-change checks. [Evidence and limits](benchmarks/results/selfcheck-chrome-521-20261105.json).

Release 0.5.520 gives clearer tool-argument errors without changing validation or permissions. It keeps the requested operation when choosing an error explanation and handles boolean and numeric alternatives correctly. A reviewed donation also keeps invalid public API catalog numbers from replacing the last good snapshot. The affected cloud suite passed 217 tests with 57 subtests; the catalog suite passed 35 tests. Whole-app autonomy remains under qualification. [Checks and limits](benchmarks/results/diagnostics-catalog-520-20261105.json).

Release 0.5.519 corrects a slow CPU fallback in the local music decoder. Fresh owned music engines apply a verified float32 CPU method inside their own process and restore the original device and number format afterward. Startup locking also covers launch and HTTP readiness. The real Python 3.11 startup path passed against original and already corrected source snapshots, following a compiler-context bug caught during qualification; 173 music and radio tests passed. Running engines continue unchanged until their next start. A separate recovered three-minute instrumental completed in 65 seconds on this PC; vocals, full-song CPU speed and automatic stuck-engine recovery still need verification. [Evidence and limits](benchmarks/results/music-engine-519-20261105.json).

Release 0.5.518 creates new Toolbox and Media environments from a pinned app-owned Python 3.13.18 runtime with SQLite 3.53.1. Setup verifies actual module and DLL origins and eleven critical file hashes before continuing. Both environments exist before the installer can open pending account connections. A durable pending marker prevents failed or interrupted creation from being accepted on retry merely because Python starts; incomplete folders are preserved. Existing working runtimes are retained, and existing-runtime migration remains separate unfinished work. [Checks and limits](benchmarks/results/owned-runtime-518-20261105.json).

Release 0.5.517 adds tested project-local arithmetic and JSON helpers. Once a helper and its consuming step are saved, the worker computes real results and yields at the next unfinished step without another model call. Immutable records survive tested process crashes, while changed instructions, interpreter code or helper records invalidate reuse. Progress publication rechecks controls and the exact tested helper; ordinary tool declarations retain their previous size. Passing supplied examples is not general correctness or whole-project completion. [Validation and limits](benchmarks/results/expression-helpers-517-20261105.json).

Release 0.5.516 keeps automatic visitor introductions steadier when facial features drift while a person remains in the same position. Current recognition still supplies names, while each known name has its own 120-second greeting cooldown. Temporary geometry continuity is rechecked when greeting: a newly visible second person, a moved face, ambiguous overlap or a genuine departure cancels suppression. Camera startup uses plainer wording, and its initial frame-rate sample is labeled as a startup sample. [Tests and limits](benchmarks/results/visitor-continuity-516-20261105.json).

Release 0.5.515 removes the separate setup GPU load when opening Animated Cale. The launcher still verifies files and runtime setup; the live camera worker validates the pinned runtime and passes a real inference probe before readiness becomes true. No old readiness marker substitutes for testing. A real local test verified one engine load and one inference probe, then produced 168 avatar frames over 35 seconds; readiness remained false until the probe passed. No 30 fps improvement is claimed. Static illustration blend terms are cached without changing output pixels. A task-planning bug is also fixed: independently persisted repair/step counters can no longer permanently hide discovery tools; the declared fifth step turn restores the ordinary baseline without renewing repair budgets. [Qualification and limits](benchmarks/results/camera-startup-tools-515-20261105.json).

Release 0.5.514 drops camera frames older than 750 ms before spending GPU work and rejects renders that became stale during inference. A stalled camera returns to neutral artwork instead of leaving an old expression looking live. Fresh delayed renders remain usable. Model routing now compares recent output tokens against total response time, including loading and prompt processing, and preserves explicitly requested reasoning effort. Pinned Ollama 0.40.13 required/named tool requests can use the native context-aware transport; original host validation still enforces their contracts. Both Ollama endpoints discard some JSON Schema keywords, so this is not full server-side schema enforcement. A live local test produced 161 avatar frames over 35 seconds, with roughly 148 ms median render time; no 30 fps or end-to-end speedup claim. [Tests, measurements and limits](benchmarks/results/routing-camera-514-20261105.json).

Release 0.5.513 records actual verification evidence in project SQLite memory and checks that it still matches the current instructions, files, checker and saved tests. For supported explicit save-across-reload requests, finishing now requires a measured browser reload that retains the changed result; requested deletions need their own retained-deletion evidence. A successful command or a completed database step alone cannot supply that proof. Browser dependency tracking supports larger local asset sets without filling chat with file lists. Unused build folders are distinguished from dependencies the browser actually loaded, and source files are rechecked after a long content scan. Large cold scans can reach their bounded time limit and remain unverified; this is a narrower behavioral guard, not a general semantic requirements auditor or a faster-build guarantee. Voice-cache recovery, “no rap” detection, optional browser-preview deadlines and companion-version parsing also improve. [Qualification and limits](benchmarks/results/verification-513-20261105.json).

Release 0.5.512 fixes four reproduced recovery/data-preservation bugs: a failed FFmpeg repair-thread launch can retry immediately, a scanned PDF keeps its readable native text if temporary storage fails, public API cache freshness belongs to the file handle actually read, and failed music-mastering fallback cleanup preserves a different replacement destination. Each donated regression failed against public 0.5.511 before its fix; the combined affected suite passed 75 tests with one existing Windows symlink skip. [Evidence and limits](benchmarks/results/maintenance-512-20261104.json).

Release 0.5.511 adds **Animated Cale** beside the camera controls. Turn the camera on to drive the illustrated beach avatar with your expressions and head movement. First use installs a pinned, isolated LivePortrait/ONNX CUDA profile and tests actual inference before showing it as ready. Animated mode displays generated artwork only; setup errors and lost faces use neutral artwork. Camera Off stops its owned workers. Local music/image GPU work temporarily pauses avatar inference, and model routing leaves headroom for its measured GPU allocation. The current backend requires the managed NVIDIA CUDA Media runtime; unsupported computers report unavailable without showing raw video. This PC rendered about 9 full frames/s, with a 512-pixel neural face inside the 880×360 beach scene; this is not native 4K avatar detail. Optional **Share avatar** sends generated frames to an already-installed OBS virtual-camera driver through a separate GPL bridge. Actual driver output passed locally; third-party meeting apps and virtual-device readback have not been qualified. [Implementation, model provenance and measured limits](benchmarks/avatar-decoder/README.md).

Created by [Cale Media](https://calemedia.com/). New general tasks use Cale's native worker with Ollama and the existing model router; older tasks can still finish through Hermes Agent.

Release 0.5.510 adds **Game library** to **Create → Game**. Browse all 182 entries from the pinned public collection, search or filter by genre, and view previews. **Edit a copy** starts a normal saved task that imports published source into your project and makes your requested changes. **Rebuild from reference** creates an original game when only a demo is available. These options preserve your Home attachments and existing project files. The first import needs internet; verified cached source works offline. Large source downloads show real transfer activity and can resume their owned staging files. Imported code is inspected before execution; downloading source never counts as completing gameplay checks. All 22 declared source links were imported in validation, including two large projects and identical fresh offline copies. That proves source availability, not that every game already runs or has the same license. [Evidence and limits](benchmarks/results/game-library-510-20261104.json).

Home now places chat and its composer above compact task cards, with responsive columns, progress rings, tokens, time and existing task controls. The webcam captures independently of face recognition and requests native 4K on compatible USB cameras; it reports the actual device mode rather than labelling upscaled video as 4K. The compact preview deliberately uses fewer pixels. Some 4K webcams require a USB3 cable and port to expose 4K modes.

Release 0.5.508 separates the desktop into **Home**, **Music Studio**, and **Create** with a dark graphite, cyan and coral design. Home keeps chat, Cale and task progress together with a composer that stays visible while scrolling. Music Studio has two real audio decks, master/deck levels, manual crossfading, Auto-DJ, actual PCM WAV waveforms, stations and a song prompt. Live EQ is not available. Create starts game/app/website tasks through the existing task pipeline and embeds their previews, including reopened finished projects. Prompt failures retain the text; dedicated studio/build requests do not silently consume Home file selections. [Validation and limits](benchmarks/results/nightshift-508-20261104.json).

Qualified local text/tool requests now use Ollama native chat with context reserved for that request, while keeping the approved model context ceiling. Unsupported request features retain the existing compatibility path. Tool history, JSON output and actual usage measurements are preserved; this is not a claim that every local model fits a GPU or that unfinished tasks are solved. The [local context evidence](benchmarks/results/native-context-508-20261104.json) includes both successful wire checks and the still-incomplete app-building continuation.

Release 0.5.507 adds four visible first-install connections: **ChatGPT, Claude, Grok and OpenRouter**. You can choose a connection while downloads continue; Cale opens the selected flow when the account screen is ready. ChatGPT uses its existing sign-in. Claude and Grok install checksum-verified, unmodified official Windows agents on demand and use each person's eligible subscription, with explicit permission for task use. OpenRouter retains its own account/free-route flow. Subscription agents can receive bounded project steps through `delegate_subscription_task`; their output remains an agent report and Cale must still run the original project checks. Local-only, free-cloud-only, offline and project restrictions remain in force. Sign-in helpers run hidden; GitHub device codes appear in Cale with Copy code and browser buttons instead of a command window. No shared keys are shipped or API billing enabled by these subscription connections. See the [connection validation receipt](benchmarks/results/first-install-connections-507-20261104.json).

Release 0.5.506 puts donation progress in the main chat: which public code area Cale is reviewing, which enabled AI source it uses, and a clickable GitHub link after a proposed improvement is actually submitted. Updates are grouped, preserve older chat position, and use the existing history and voice controls. Quota failures no longer delay another enabled donation source, and invalid proposals pause only that source/work area. Local reviews use approved quick models with bounded output and retain complete source/test work areas. Four reviewed donor fixes are included. The [validation receipt](benchmarks/results/donation-chat-506-20261104.json) records the checks and limits.

Release 0.5.505 avoids blaming a local model when it discovers that its internal PLAN or PROGRESS note has not been created yet. Those reads still report failure and the work remains unfinished, but they no longer add model-quality penalties that can trigger a needless model switch. Genuine failures and existing penalties remain. The [validation receipt](benchmarks/results/note-feedback-505-20261104.json) records the regression and routing controls.

Durable capability recovery from 0.5.504 remains included: when a task encounters a missing tool or dependency, Cale can search its local tools, create and test a small helper, then retry the original acceptance checks. Recovery attempts survive restarts, and an eligible focused checker repair takes priority over broad source changes. Permission/account barriers, resource settings and original completion checks stay in force. This helps Cale recover; it does not guarantee correctness for every possible input. The simpler home page scrolling from 0.5.503 and model-health fixes from 0.5.502 remain included.

Model-building donations from 0.5.501 remain available. Select Local and “Also train small specialist models on CPU” in Donate AI power. Experiments use pinned public data, keep weak results local, and submit screened candidates as inactive draft contributions for review. Training time is shown separately from inference tokens. The [training evidence](benchmarks/results/model-donations-501-20261104.json) records the real worker check and its rejected candidate; no newly qualified routing model is installed.

The native worker saves each model/tool step, resumes after a crash, keeps a project plan and progress file, and can run the project's verification script through a direct contained tool before handoff. Its model tier is chosen from the saved user request, not the worker's generic “begin” message. The first model call includes short project rules and the saved plan and progress excerpts, avoiding repeated reads before useful work starts. For a simple interactive HTML app, the worker runs browser cases before creating a verifier and preserves the generated browser verifier once it exists. The supervisor independently reruns that script before marking work finished. It can search public webpages, edit project files, run commands, and discover Cale's installed local image, audio, video, camera, memory, and training tools. Existing Hermes tasks retain their board history during this migration. The supervisor stores worker process identity separately from the step checkpoint, so a worker that saves an early result cannot have it overwritten during startup. The installer still includes Hermes for that recovery path and for capabilities that have not moved to the native worker yet; signed-in Chrome/DevTools sessions remain one such gap. No OpenClaw runtime or source code is bundled.

For reports based on a local document, Cale can check exact source excerpts against the report and verify simple added or subtracted dollar amounts. A passing check saves a read-only verifier tied to the source file's bytes, so changing either the report or source invalidates that check. It only verifies the facts supplied to it; other claims still need review.

Automatic native-worker routing remembers repeated local tool failures across
checkpoints. After two failures it prefers different physical model weights
within the already eligible local candidates, so switching aliases for the same
weights does not count as trying another model. Reads and writes alone do not
erase this feedback; verified recovery or new user instructions clear it.
Explicit model selections, local/cloud modes, hardware checks and context limits
remain authoritative. Deferred models remain fallbacks when alternatives cannot
respond. This is a repair preference, not a guarantee that another model will
solve the task or finish faster.

The native worker can check its current SQLite unit with
`task_steps {"action":"complete"}`. Cale binds that shorthand to the exact
unit and acceptance check shown before inference; explicit wrong IDs and
changed units/checks are rejected. Source edits are allowed, but the saved
acceptance check must actually pass with unchanged project files during the
check. Completing one unit prevents further actions from that same response
from advancing the next unit. Both explicit completion and automatically
recognized checks commit progress only if the acceptance identity is still
current, preventing an older result from completing a revised check. Direct
tool dispatch outside the native worker still requires an explicit step ID.

New step-tool requests advertise `steps` as an actual JSON array, rather than
offering a string alternative that can encourage escaped-JSON loops. Existing
valid JSON-string plans still decode through the compatibility dispatcher.
When a decoded local tool call fails its offered argument schema, the router can
make one correction attempt on that same model, using the schema rule and field
path instead of repeating the malformed value. It offers only the rejected
tool and preserves explicit tool-choice restrictions. Calls rejected by validation never reach
the worker. This extra local attempt must fit the configured context, stops on
cancellation, and is capped at 4,096 output tokens and 120 seconds. It does not
add retries to cloud routes or change account permissions. Syntax-broken outer
JSON and unavailable tool names continue through the existing fallback path.

Structured browser steps that send input must establish an observed effect;
unchanged score labels cannot by themselves complete a movement unit. An
invariant-only sequence can use a freshly replayed positive control for a
matching assertion target in the same HTML. Static scaffolding checks need no
input effect. The saved-suite receipt binds input counts and changed targets to
its test data, user instructions and page bytes, and rejects HTML changes between
sequences. This is a coverage check, not proof that the cases cover the whole
request or that their expectations are correct.

For interactive single-file HTML apps, the native worker can use an offline Chromium check that fills real inputs, inspects visible results and errors, and checks mobile width. A passing check saves a repeatable browser verifier. The worker must rerun the check after changing the page before it can finish. This catches behavior that a source-text scan misses; it does not establish that an entire website is correct or visually polished.

Canvas apps and games can also expose their actual live state for passive checks.
For example, `check_webapp` can hold ArrowRight and assert
`state={"game.player.x":{"gt":300}}`, then press Space and assert
`state={"game.bullets.length":{"gte":1}}`. Exact primitive values and finite
numeric bounds are supported. The checker reads own plain data properties,
without calling functions or getters, and records only asserted fields that
actually changed after input. Cases remain in the repeatable verifier and can
serve as SQLite step checks. A passing state check does not establish rendering
quality, complete gameplay correctness or that a separately exposed test copy
matches the actual game; independent integration checks remain necessary.
The checker handles letters entered for a number input as the empty value a browser would produce and reports that normalization. A live task browser is closed before the separate offline checker starts, so opening a website earlier in the task does not break local app checks. Rejected local `file://` browser URLs no longer start a browser session. Failed text assertions show the page's actual value to help the worker correct either the page or its test.
When someone adds instructions to a task, Cale discards the earlier verifier and browser-check results before continuing. The revised work needs fresh checks, even when an earlier version of the task had passed.
If an instruction arrives while the model is choosing a tool, Cale drops that older tool plan before it runs and reads the new instruction. Instructions arriving between steps are included in the next model call.

For common CSV cleanup, the native worker has a project-contained `transform_csv` tool that uses a real CSV parser for trimming, case changes, phone digit cleanup, row filtering, and deduplication. It preserves the source and creates a read-only verifier when the task has none. That verifier also re-parses both files and checks every output row against the saved transformation rules. A verifier that changes project output cannot pass the worker's legacy command check. When a new request clearly asks to create one CSV from another with supported cleanup operations, Cale asks for the structured tool call before general work begins; unsupported operations, existing output, restrictive project instructions, and queued follow-ups use the general worker. The local model still reviews the output, so individual task time can vary.

Skills and tool descriptions are also indexed in a local SQLite FTS5 database (`LongTasks/capabilities.sqlite3`). Setup builds it before opening the desktop; launch and the supervisor refresh it automatically, including locally edited skills. The native worker can search that catalog and load a relevant skill without scanning every Markdown file on each task. The index is rebuildable from the installed files and tool registrations; it contains no account keys or project documents. The readiness checklist queries the database and offers automatic repair if it fails. Large optional model weights still download according to the PC's available resources, so a capability appears as ready only after its runtime or model has actually passed its check.

## Cale Radio

The homepage radio makes original three-minute tracks locally. **Cale Club**, **Cale Country**, **Cale Rap**, **Cale Pop**, and **Cale Rock** sing about Cale Media, local SEO, and marketing. **Chill** is instrumental. Choose a station and press **Play**, or use **+ Station** to name your own station and describe its sound. **Edit** changes its prompt and vocal setting. No radio account is needed. The exact old instrumental Cale Club preset is upgraded to vocals; custom station prompts are preserved.

The player shows real track playback time, a separate next-track rendering bar, completed track count, **Total local tokens** across this installation, and **Radio tokens** from attributed local lyric calls. Token counts come from the usage ledger; audio generation has no invented token count. Radio-specific attribution begins with this update because older versions did not retain radio station IDs on usage events. Missing usage history displays a dash. Mute restores the previous volume when unmuted.

The radio plays one track while generating up to two more, with one radio render active at a time. Ready audio immediately loads into a second Windows audio deck at zero volume. Eight seconds before the current track ends, the next starts and both decks blend with equal-power volume curves. **Mix next** starts that transition early. Pause freezes both decks and the fade; mute and volume affect both. The blend is a timed crossfade; song tempo remains as generated. It plays the existing files without creating duplicate song or video files.

Normal song renders take priority over starting another radio render. If generation falls behind playback, the player blends into a cached repeat; an arriving fresh track can replace that repeat after its fade completes. The cache protects files held by outgoing and preloaded decks. Rendering speed depends on the PC and whether lyrics are needed; a three-minute render is not guaranteed to finish in under three minutes on every device.

**Pause**, **Mix next**, **Stop**, and volume stay on the homepage. Stop ends playback and further generation; a render already in progress can finish in the cache. Stations and queue state live in `LongTasks/radio.sqlite3`, audio in `Radio/audio`. Only radio-owned cache files are pruned; uploaded references and ordinary song tasks are untouched. Playback is off on each app launch and setup/updates hold new generation.

## Donate AI power

The header's **Donate AI power** button lets people opt into **local models**, **connected free cloud**, **ChatGPT plan/credits**, or a combination. GitHub is required to submit fixes; local donations need no ChatGPT account. The free-cloud button reuses OpenRouter's browser connection, and existing confirmed free providers are detected too. Free-cloud donation requests cannot fall back to paid inference or local models; local donation requests use only loopback Ollama. Sources rotate between attempts. Donations are off by default, and changing the selected sources/intensity stops the old grant until the person starts again. Existing ChatGPT-only consent stays ChatGPT-only after updating. Keep the desktop open or minimized for contributions.

| Intensity | Idle before starting | Minimum rest | Attempts per 24 hours | Open drafts |
| --- | --- | --- | --- | --- |
| Slow | 5 minutes | 1 hour | 1 | 1 |
| Medium | None; runs alongside your tasks | None after successful work | No app cutoff | No app cutoff |
| Full Blast | None; runs alongside your tasks | None after successful work | No app cutoff | No app cutoff |

Medium now uses the previous continuous Full Blast pace: one deep review at a time, including while you use Cale. Full Blast allows up to six concurrent reviews when ChatGPT is among your explicitly donated sources, with a larger public-source budget. Local and free-cloud work remain single-lane; local work waits while music/image work holds the GPU. Slow uses smaller eligible local models and two CPU threads; Medium uses up to half the CPU threads and Full Blast uses all CPU threads for its one local lane. These are scheduling choices, not a promised percentage of a subscription allowance. Hardware fitness and account quota limits still apply. Installation and updates temporarily suspend all donation work, which then resumes automatically. Local mode blocks cloud donations, Free cloud only blocks ChatGPT donations, and offline mode pauses all contributions because GitHub is required.

The worker reads `donation-tasks.json` and its approved source files from an exact commit of the public Cale repository. Medium and Full Blast also discover bounded production/test pairs in that public revision, excluding authentication, donation, accounting and update-policy modules. Transactional claims reserve each area across public revisions, so merging an unrelated fix cannot start duplicate reviews. Submitted drafts stay reserved until an authoritative GitHub pull-request check confirms closure; failed or incomplete checks retain the reservation. All lanes share a bounded three-draft refresh every 30 seconds. Expired in-flight attempts and recent unchanged reviews can retry after 30 minutes, while different areas continue in parallel. All lanes share account cooldowns after rate-limit errors. It retries automatically if accounts, GitHub or the queue are temporarily unavailable. It never includes user documents, private projects, chat history, or credentials in the model prompt. It applies bounded exact text edits in memory and parses Python syntax without executing generated code. Production changes must include a regression-test edit. A proposal goes to a new branch in the contributor's GitHub fork (or a separate branch for the repository owner) and becomes a **draft pull request** in CaleMediaDev/cale-ai-workbench. The automation never changes `main`, approves changes, merges, or releases. The owner reviews the code and runs relevant tests before accepting it.

**Total tokens donated** is this installation's persistent sum of provider-reported input and output usage, including reviews that produce no change or proposals that fail validation. The dialog breaks totals down into Local, Free cloud and ChatGPT. Separate receipts count reported free-provider fallback calls without double-counting completions. Partial requests without reported usage are not estimated. In Slow, returning to Cale cancels further work and closes a streaming local request; an in-flight request can still consume resources. Stop and opt-out cancel all lanes in every mode. The local ledger is `LongTasks/donations.db`; credentials remain in their existing per-user protected stores.

Maintainers can change the public task list to direct contributions and use **View contributions on GitHub** to review drafts. These proposals have passed exact-edit and Python-syntax validation only; donor PCs do not run generated tests or code.

The **Leaderboard** ranks GitHub contributors by distinct donation PRs merged into the canonical main branch. It uses GitHub author IDs and actual merge records, not token totals or client claims. Open, rejected and unmerged PRs receive no points. Viewing public rankings requires no extra account. Rankings are cached locally, work offline from cache, and explicitly show partial history while older pages are still loading.

## Install

**[Download Cale AI for Windows](https://github.com/CaleMediaDev/cale-ai-workbench/releases/latest/download/Cale-AI-Workbench-Install.zip)**

1. Download `Cale-AI-Workbench-Install.zip`, extract it, and open the single **Install Cale AI.exe** inside. You can also download `Cale-AI-Workbench-Setup.exe` directly.
2. Click **Install**. Watch the progress bar as Cale sets up your PC, then click **Open Cale AI** when it is ready. Technical logs are tucked under **Details**.
   Once the basic app tools are ready, **Connect accounts** becomes available. You can connect ChatGPT, OpenRouter, or another supported provider while the model downloads continue. Accounts are optional and use your own sign-in or API key.
3. Type into the chat. Simple questions get a local answer; requests to build or research something become saved tasks with progress shown below the chat. Finished task cards have **Reuse prompt**, which restores the original request to the chat editor for changes before sending. Cale can search older user chat messages locally when a new question refers to a detail beyond the recent conversation. No account is needed for the default experience.

**Model activity** opens from the top of My tasks, so a long task list never hides the graph. It separates models by provider and shows the ChatGPT token total recorded by Cale beside the button. These are provider-reported tokens for calls made inside Cale, not the account-wide ChatGPT plan balance; calls whose provider omits usage cannot be counted as tokens.

If a previously blocked Hermes task starts a new worker run, its card returns to **Working** automatically. A genuine unresolved block remains visible until a later run actually starts.

For local music, ACE-Step now loads its optional music-planning model on GPUs its own hardware check supports. When that model is ready, generated songs use its enhanced planning path; on smaller PCs or if model loading fails, the existing direct generation path remains available. The app still verifies audible vocals before claiming that a vocal song is finished; an instrumental breakdown inside a vocal brief no longer turns the entire song instrumental. On launch it also checks previously completed, unaccepted music cards once for this older routing error and starts a vocal take when the original brief clearly requested singing or rap. For requests built around short vocal hooks and long instrumental drops, the replacement keeps that arrangement and writes a shorter hook sheet instead of full verses.

Music jobs reconcile the agent's tool arguments with the written request: a clear singer or rapper request overrides an accidental `instrumental=true`, including when a queued job from an older version resumes. A request explicitly for an instrumental track remains instrumental. A standalone music worker also delays source updates until its render exits.

For a requested song longer than a supplied audio clip, Cale uploads that clip as a style reference to ACE-Step's text-to-music path. The cover path follows the source clip's length and cannot extend it. Style-reference jobs check the finished duration and, when vocals were requested, check the rendered audio for sung words before reporting success. A style reference guides the new recording; it does not guarantee the same melody or voice.

Music countdowns use recent completed renders of the same kind. Longer style-reference songs are timed against style-reference jobs, rather than quick covers of short clips; when there is not enough comparable history, the task card says it is estimating instead of showing a misleading short countdown.

Music references can also be MP4, MOV, M4V, WebM, or MKV videos with an audio track. Cale extracts the audio locally into a temporary WAV for the music engine and removes that temporary copy after upload. The video frames are not sent to the music engine, and a video without readable audio returns a clear error.

Phone recordings and other compressed audio in M4A, AAC, OGG, or OPUS format can also be used as references. Cale converts those files locally to a temporary WAV for upload; the original remains untouched.

In the main chat, attach one supported audio or video clip and ask Cale to use it as a reference, cover, remix, or longer version of a song. Cale starts a durable local music task directly and keeps the requested length, lyrics, and vocal preference. Requests for a music video, captions, or edits involving multiple files still use the general task agent.

Natural requests such as “I want a three-minute version of this song” and spoken lengths such as “one minute thirty seconds” also start the corresponding music job. Questions about how to make music remain ordinary chat questions.

The installer downloads the runtime, tools, browser, image/voice models, and model files. It creates a desktop shortcut with the Cale Media logo, which also appears on the app window and taskbar. No Python, Git, Node, terminal commands, or API keys need to be installed manually. The ZIP alternative contains `Install.cmd` with the same setup screen.

GitHub CLI is installed automatically. The button opens GitHub's own browser approval flow; GitHub may ask you to confirm a device code. Your login remains on your PC and is never bundled with the public installer. When you ask the AI to save or publish a project, it can create a repository and push your code. [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages) can publish static HTML/CSS/JavaScript sites from suitable repositories. It does not run a backend, and [GitHub's Pages rules](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits) rule out using it as free hosting for an online business, ecommerce site or commercial SaaS. Repository and storage limits still apply.

The **Supercharge my AI** screen recommends the [NVIDIA DGX Spark](https://www.nvidia.com/en-us/products/workstations/dgx-spark/) for people who want to run much larger models locally. Its Amazon button uses Cale Media's referral link. DGX Spark runs Linux on Arm, so this Windows x64 Workbench installer does **not** run on it today; the hardware recommendation is for separate local AI workflows. No cloud subscription is needed for models that run on the device, though downloads and online tools still need internet.

On launch, Cale stays silent until base setup and an actual local render using his own voice clone have passed. A bounded, cached render probe checks the selected model revision and audible output without playing sound or recording the microphone. The full software self-check runs separately in the background; microphone readiness does not gate working speech. The home screen shows voice readiness; chat and task updates remain visible while speech is unavailable. Its greetings rotate between launches, and its task, idle, and stuck comments vary while staying grounded in actual progress. Once speech is ready, it explains that the mic starts on and continuous voice control only responds to instructions beginning “Hey Cale.” Click **Mic Off** to stop listening; click **Mic On** to resume. If a clip catches just “Hey Cale,” it replies aloud in the owner's voice and leaves time for the next spoken instruction. The shortcut records a single instruction without a wake phrase while the app is focused. The compact chat box accepts pasted multiline instructions or lyrics: press Enter to send, or Shift+Enter for a new line. Voice capture allows up to 20 seconds for a full instruction, but ends after a pause and restarts quiet hands-free listening after four seconds. Voice detection checks sustained audio energy so idle microphone spikes do not masquerade as speech. If the selected microphone captures almost no sound, the voice status names that device and suggests checking its mute and Windows input level; audible but unrecognized speech names the device and asks the person to speak closer or repeat the wake phrase. The screen also shows task progress, and voice updates are paced so a busy download does not speak every percentage point.

Cale's spoken updates use his own offline voice clone. On PCs with at least 6 GB of GPU memory, a background task downloads the Qwen3-TTS 0.6B clone and prepares it using Cale's bundled reference clip. Spoken updates wait for that clone and the readiness checks; a PC that cannot prepare the clone remains quiet. If cloned synthesis fails or the GPU is busy rendering music, Cale keeps the response in chat and does not substitute Kokoro or Windows speech. Microphone turns still interrupt synthesis and playback. The talking panel shows an illustrated beach character animated while speech plays; it does not show the owner's real face or claim exact lip synchronization. Voice files and synthesis stay on the PC.

Ask Cale to “say that again” or “read the rap aloud” to replay an earlier chat reply. A requested replay shows progress in the voice status and saves a completion or failure note in chat. That note is not itself used as the target of the next replay.

Click **Mute Cale** to stop spoken replies immediately while chat and the microphone keep working. Click **Unmute Cale** to restore speech; the choice is remembered on later launches.

The fast local model adjusts Cale's conversational tone to the latest person's own words. Friendly speech gets a warm response; rude speech can get a dry, cheeky answer before Cale helps with the request. This also applies to a visitor speaking after a camera greeting. The model resets its tone each turn, so a rude remark does not make it hostile to the next person. It does not infer attitude from a face, quoted material, or a prior visitor, and tone does not change facts or task status.

Camera Greeting tracks separate visible faces when the local face models are ready. It can greet a new arrival while someone else remains in view, and speaks one greeting for a group entering together. Face learning still requires a single visible person so a volunteered name is not assigned to someone nearby.

Camera greeting stops when the switch expires, the window closes, or its window process exits unexpectedly. The camera worker checks the exact process that enabled it and exits even if a network stream read is stalled.
Camera Greeting says hello once per stable arrival. A person who remains in view is not greeted again when the cooldown expires; after they leave and return, a new greeting is allowed only if the cooldown has already passed.

After an update, Cale runs its packaged regression checks while tasks are idle. If someone starts a task mid-check, the check yields CPU and resumes on the next idle supervisor pass without counting that pause as a test failure.

The app checks Cale Media's GitHub releases at startup. When a newer release has a published SHA-256 digest, it downloads and verifies the ZIP in the background. It installs source updates only after current tasks finish, keeps the previous source in `Updates/backups`, preserves projects and credentials, then reopens. The installed-version marker changes atomically only after the source swap; a failed swap restores the previous source and version. A public model recommendation feed can add official Ollama models that fit RAM and disk; each new model must pass a local tool-use check before the router uses it. Recommendations cannot reuse an existing model alias, and pulled weights are probed before a candidate alias is created. New weights use a new profile and alias so a failed upgrade leaves the working model available. Qwen 3.6 uses its verified no-thinking mode for tool calls so its reasoning trace does not consume the tool-call budget.

New setups use Ollama's [native Windows installer](https://docs.ollama.com/windows), which provides its own update path. An older Workbench install that uses the standalone Ollama ZIP migrates to the native app in the background once tasks and model downloads are idle. Other bundled runtimes remain pinned to tested versions; the source updater does not yet replace them automatically.

The main task context is chosen when a worker starts from the local model's advertised window and the PC's RAM. It starts at the 64K minimum needed by Hermes, then grows as far as the model and memory estimate allow; on the development PC's 128 GB RAM and Qwen Coder Next, that is 262K. Larger optional local models also use their native window when memory permits; existing aliases with smaller windows are widened only while no project worker is active. The dedicated fast alias stays at its tested 64K window so short requests do not reserve a huge context. The router estimates each request's size: short requests can use a smaller, faster local model, while requests that need more context stay on a model with enough room. For balanced work it can promote an installed, approved newer model in the same size tier after a real local tool-call probe, retaining the older model as fallback. A model already loaded in memory gets priority only when its measured generation speed is reasonably close to the preferred model; a warm but much slower model stays a fallback. It skips installed models whose known context windows are too small and explains the limit if no local or connected model can answer. More context uses more memory and may slow generation, so this estimate is a practical capacity setting rather than a promise that every prompt at the limit will fit.

A straightforward chat request such as “make an image of a dog” goes directly to the installed local image maker. It saves and opens the PNG without creating a weeks-long agent task. Image edits and requests with attached source files still go through a task so their requirements and files can be inspected.

**Windows x64, 16 GB RAM minimum.** Automatic setup chooses a smaller model for smaller PCs. The large coding model needs about 96 GB RAM and downloads roughly 52 GB. The installer also downloads a 9B vision model, a diffusion image model, speech recognition weights, and a small training base. Allow at least 32 GB more disk space than the chosen coding model's download size. The initial download needs internet; installed local models can run offline. Public web tools still require internet.

This is an **early preview**, with an unsigned Windows installer. It has been developed and tested on Windows with an RTX 3060. Other hardware configurations and uninterrupted multi-week operation have not been verified. The header shows live CPU, RAM, and NVIDIA GPU usage with RPM-style rings that light from green through amber to red; unsupported GPU sensors show a dash.

## What it can do

- Repair one mistaken saved browser assertion using the failure's sequence, case and suite checksum. `check_webapp` retains input actions, other cases and unrelated assertions, then reruns the browser checks and verifier. Stale checksums, changed instructions and custom verifiers are protected. Failed cases carry their actual inputs into SQLite checkpoints, including after setup-case normalization; bounded JSON reports preserve case references through log compaction and PowerShell encoding. A failed attempt to create the first SQLite plan remains a completion issue until a real plan exists, rather than silently becoming “done.” These controls do not determine whether a proposed assertion matches the user's intent.
- Before completing an owned browser-check task, compare concrete invalid numeric examples in the request and full follow-ups with retained input cases. Static number inputs are matched through unambiguous IDs and labels, and numerically equivalent values count as the same example. Missing cases prevent a verified handoff until tested. This deterministic presence check uses no model calls; it skips ambiguous controls, nonnumeric fields, range descriptions and mixed valid/invalid clauses. It does not prove arbitrary natural-language coverage or assertion correctness. The browser checker also accepts a setup-only case immediately followed by an assertion-only case, preserving action order. An empty expected string checks exact cleared DOM text; it never acts as an empty substring that matches everything.
- Store offline browser checks directly in SQLite steps using `check_tool='check_webapp'` and `check_arguments`. The owned verifier retains up to 32 independent sequences (128 total cases) across pages and viewports; each sequence starts from a fresh browser context. Reusing the same input sequence corrects its expectations. Saved suites are tied to current user instructions and checksum-pinned, and a regression in an earlier sequence fails a later check. Successful replay clears diagnostics only for pages actually verified. Existing custom verifiers are preserved; legacy single-spec scripts still run, and their first accumulated save starts a new request-bound suite. Test coverage still depends on the cases the model selects.
- Retain bounded failure counters in its fresh database checkpoint so repeated failed steps still request deeper reasoning after tool history is shortened. Two failed checks on the active step, or three identical unresolved checker failures, trigger the existing repair route. Passing the step clears its counter; a new user request starts fresh checker counts. Structured failed verdicts are recognized alongside command errors. This changes the routing signal, not the configured local/cloud modes, model eligibility or account limits; it does not guarantee a stronger model will solve the problem.
- Save small implementation steps and their acceptance checks in each project's SQLite database. Python function checks can store concrete inputs, expected JSON values and expected exceptions directly, avoiding fragile shell one-liners. Checks accumulate across functions and files, and the generated verifier reruns them against current source before completion. Failed checks retain diagnostics in the next checkpoint. Existing command-based steps migrate without losing progress. The model still chooses the cases: passing them does not prove coverage of every requirement. Larger applications, classes and I/O workflows continue to need suitable integration tests. These checks execute project Python using the normal contained command runner; they are not a security sandbox.
- Work through a saved project plan, build in milestones, run tests, and recover from saved progress. Follow-up instructions are kept in a project file so weeks of changes remain readable without filling the model's first message. A fresh worker sees the first acceptance checks and latest progress even when those files have grown large, then re-reads the full files before relying on them. The SQLite checkpoint chooses the newest explicit next action in the progress log; a later plan edit can supersede it.
- Load relevant agent skills on demand. The bundled Hermes agent already includes dozens of skills; Cale adds seven pinned, locally installed workflows for context management, APIs, frontend work, performance, security, observability, and launch checks. Skills are instructions and references, not extra models or cloud accounts. Updates preserve any locally edited skill files.
- When a task needs a skill that is missing, Cale can search the public skill registry, inspect a relevant result, and install it through Hermes' built-in scanner. A blocked skill is not force-installed. Registry search needs internet; installed skills keep working offline.
- Create a local SQLite memory database automatically in each project. It indexes plans, progress, project documents, and selected code for fast full-text search; the agent can save typed requirements, decisions, lessons, app components, and reusable skill recipes. Files attached in chat are indexed automatically in a separate private, local full-text database for later tasks. The offline vision and speech models describe images, transcribe recordings, and summarize sampled video frames for that index; those descriptions may miss details and must be checked against the originals. Searches refresh changed files and remove deleted ones. Project files and attached originals remain the source of truth. No database account, server, or extra installer step is needed.
- On launch, scan accessible local drives quietly in the background into a separate SQLite filename and folder index, then refresh it daily. A completed catalog remains fresh across app restarts, so reopening Cale does not immediately rescan the whole PC. Personal folders are scanned first. Ask chat to find a file by name, click a found path to reveal it in Explorer, or let an agent use `search_computer_files`. Searches try all words first, then rank close matches when a natural phrase contains extra words. The index stores paths and names, never file contents; content is opened only when a task needs the original file. Completed refreshes remove deleted entries. If the index drive has less than 5 GB free, scanning pauses while existing search results remain available. Network shares and unreadable folders are skipped. Indexing does not appear as a task card.
- Compress conversation history automatically as its context fills. Project plans and progress files remain on disk for a fresh worker to resume; summaries can still omit details, so important requirements belong in those files. Short app builds use the strongest model for initial planning, then the middle local model for routine tool steps; detailed or explicitly complex projects keep the strongest model. The Hermes main alias retains at least 64K context. Native-worker routing can use a smaller fixed-window candidate only when the complete request and output allowance fit that model's actual configured context.
- Track task milestones with progress bars, the current step, and an estimated countdown to review. The countdown appears after enough measured progress, excludes pauses and app downtime, and says when an estimate is unavailable. Pause, stop, delete, resume, receive new instructions, and review completed work. Chat feedback can reopen a task that is ready for review. Deleting a task removes it from the list but preserves its project files.
- Use files, terminal commands, public web search, local multi-page website crawling, browser interactions, document extraction, and database queries. The native file reader streams requested line ranges and caps unusually long lines, so a large log or source file does not fill memory or the model context just to inspect a small section. Its exact-block edit tool changes existing UTF-8 files without sending their full contents through the model; ambiguous or stale edits fail before replacing the file. Crawled pages, small browser downloads, and inspected PDF, Word, spreadsheet, and presentation text become searchable project memory. Search verifies document bytes against the saved index before returning a passage, and reading an edited document rebuilds its index. Business-phone lookup reads live pages and can use either clickable phone links or clearly labeled phone numbers printed on the page. The bundled Chrome companion also lets you attach selected signed-in tabs after Chrome approves the matching extension.
- See a daily or monthly token-use graph after the task cards in the same scrollable area, including the latest measured output tokens per second. The graph cannot push the tasks out of view when the window is short. It stores totals reported by local and connected models on this PC; history begins when this feature is installed.
- Watch CPU, RAM, GPU, drive-space, and network rings in the header. The drive shows free space and PC-wide read/write MB/s; the network shows receive/send Mb/s against the active link's reported bandwidth.
- Click **Go offline** below the network gauge to disconnect this entire Windows PC through the active physical network adapters. Windows asks for administrator approval. The same button restores only adapters that Cale turned off. Local AI and files remain available; connected cloud providers and web tools wait until internet is restored.
- Watch website and app tasks in an automatic side preview. Local web pages refresh as the project changes; desktop apps can provide screenshots, and Chrome DevTools work mirrors its current browser page there.
- Discover specialist REA and Chrome DevTools operations without loading hundreds of tool schemas into every prompt.
- Inspect JavaScript, analyze Windows binaries, and use Ghidra where supported.
- Connect your own free OpenRouter account with one browser sign-in, then fall back to local inference when limits or errors occur. No API key copying is needed.
- Optionally connect ChatGPT and explicitly enable your shared ChatGPT/Codex plan allowance for tasks. Connecting alone does not use that allowance.
- Automatically download additional tool-ready local models that fit the PC's RAM, GPU and available storage, one at a time after setup. Candidates include Qwen 2.5 Coder 7B/14B, Gemma 4 E4B and Qwen 3.8 27B. The 27B profile requires at least 22 GB reported VRAM and 32 GB RAM; it is skipped on a 12 GB RTX 3060. The 14B profile requires 11 GB VRAM; the 7B profile requires 6 GB. Real tool calls and generation speed must pass before approval. Failed tool/coding candidates get a seven-day retry cooldown.
- Game-model promotion uses local public-fixture checks, not parameter count or a leaderboard claim: two independent game-logic generations plus a complete HTML game build with a specified live-state contract. Only candidates that pass the larger build can enter automatic game selection. A single qualified model can take priority over an unqualified baseline; qualifying models are ranked by measured build and logic time. Reports match the installed weights, hardware and a 30-day expiry. Logic/contract checks do not prove visual quality, browser input behavior or that a model makes the best games generally. When no candidate qualifies, existing eligible models keep serving games. The regular hardware/context/tool gates still apply to every request.
- Interactive HTML completion requires measured consequences of input, not just passing display assertions. The offline checker records changes in asserted text/visibility and binds evidence to the HTML bytes checked. Recognized HTML game builds need at least two input cases changing at least two gameplay targets before native-worker completion. Static starting labels, key presses with unchanged outcomes, and stale checks do not qualify. This is a minimum behavior check; it does not prove that every mechanic works or that a game is polished.
- A fresh worker keeps bounded checker diagnostics in its private project SQLite database: failed cases, test inputs, actual/expected messages and JavaScript errors. These survive short-context resets and action-log trimming. The checkpoint shows three unresolved checks; `search_project_memory` with `kind="check"` retrieves others. A later passing check clears that file's failure. Repeated identical problems remain visible even after ineffective source edits. New queued user instructions hide diagnostics from the previous request. The store keeps at most 128 check records and prioritizes unresolved failures; it does not save arbitrary tool output or complete input documents.

Failed browser cases with click/key actions also report bounded native HTML
validation flags, such as a number below its minimum. This helps distinguish a
browser constraint from a broken submit handler. Inspection reads validity
without firing additional validation events or changing the page. These are
diagnostics, never grounds to pass an assertion or remove a requested constraint.
Browser checks can reload the same isolated page to test persistence, select a
dropdown by value or label, and click a unique exact button name when no ID is
available. Ambiguous buttons still fail instead of selecting one arbitrarily.
For repeated table actions, `click:[{"button":"Delete","row_text":"Paper"}]`
selects the uniquely matching row and button by exact accessible text, without
requiring generated IDs. Duplicate rows or buttons fail without clicking.

New failing cases are retained in Cale-owned browser suites. A later unrelated
passing case must rerun them and cannot clear their failure. Saving that failing
suite creates a repeatable check, not a success claim. Failed assertion revisions
do not replace earlier assertions, custom verifiers remain untouched, and checks
superseded by changed user instructions are not saved under the new request.

After a failed structured step, focused turns offer `task_steps action=revise_check`
to correct only its typed checker arguments. The saved phase, title, checker,
path and function remain fixed; ordinary revision tools return periodically.
The operation does not complete a step. Browser failures immediately include
the checksum-bound saved-case reference needed for an explicit assertion repair.
Such repairs replace the selected mistaken assertions while preserving unrelated
assertions, cases, input actions and sequences.
When an explicit owned-browser assertion revision passes, the native worker can
align its matching active SQLite check to the corrected arguments. This requires
the same request, page, input sequence and original active check; changes after
the check, stale bindings and unrelated sequences cannot advance progress. Alignment itself
does not complete the step: its acceptance check must still run and pass.

Mistaken browser input actions have a separate explicit repair mode:
`check_webapp` takes the same saved sequence/case/checksum reference,
`revise_inputs:true`, and one corrected case. It replays the real sequence,
including that case's input prefix. Only an actually passing selected case can
replace its saved definition; other cases, sequences and failed verdicts remain.
Input actions and mistaken assertions can be corrected together, avoiding a
deadlock when neither correction could pass on its own. Source changes, stale
references, changed instructions, custom verifiers and collisions with another
saved input sequence reject the replacement. A worker-bound repair can align
the same active SQLite definition without completing it. Neither the revision
flag nor its one-time reference can be stored as repeatable step acceptance.
Generated browser suites stay behind this checker API rather than raw file edits.

Clear interactive-app builds and explicit SQLite-step requests require a real
step queue before handoff. Default workers can create a small source skeleton,
inspect its actual controls, then plan acceptance using that interface. Existing
pages are preserved. SQL steps, browser interaction, and final verification are
still required; writing a page alone never completes the task. Established
starter/function planners and existing queues are retained. After a completed
workflow, a small follow-up uses its own intent.

The pre-implementation executable planner is a developer experiment, enabled
only by `experimental_initial_plan=True` in project metadata. It restricts tools
to discovery and typed planning, requires ID-based visibility targets and at
least one action/outcome sequence. Live local trials still produced invented
tests, so this experiment is off by default. The receipts are retained in the
developer benchmarks; they are not evidence of reliable autonomous app building.

Planning binds the original brief, full project rules and follow-ups to the
prompt that actually read them. Changes rebuild the briefing rather than pairing
new hashes with old instructions. SQLite checks initial-queue emptiness under
its write lock and rechecks live control/requirements before and after insertion;
stale, stopped or concurrently superseded planning rolls back. An accepted plan
ends its response batch so later calls cannot act against the previous queue.

An invalid legacy acceptance command can also trigger a bounded focused repair.
For a small, single HTML page, Cale supplies observed control IDs and the full
original request and short project rules. The model can revise only that active
definition, with its phase/title fixed. The focused prompt retains browser test
semantics: filling does not submit, clicks are explicit, and expected strings
mean literal visible text. The actual checker still decides whether behavior
passes; oversized requirements and exhausted focused attempts restore the normal
reading/editing workflow.

Failed form checks that fill known controls but never click or press a key can
also receive a short checker-only decision turn. The observed form/submit IDs
are a hypothesis, not proof that submission is necessary. The offered repair is
pinned to one saved case and cannot edit source. After at most two focused
attempts, or an explanation that source/evidence needs work, normal tools return.

A browser step now records its own batch verdict separately from the retained
project-wide verifier. Real passing checks can advance one small step while
later incomplete features remain failed; the whole project still needs the
retained verifier to pass. Input-effect, JavaScript, source-change and request
guards remain part of step acceptance. Focused repair turns show bounded
current source snippets around IDs and handlers, and use small edits for an
existing HTML page. New-file writes and periodic unrestricted turns stay available.
Repeated selector/checker failures can receive two short decision turns with
the complete original brief, current follow-ups and project rules. These turns
advertise only checker/step operations; their returned calls are validated again
before execution. Changed requirements invalidate in-flight decisions, and the
attempt reservation survives worker restarts. Ordinary task tools return after
the bounded decision or when its complete requirements do not fit.

The browser helper retains up to 32 independent sequences and 128 cases, within
its existing 64 KB specification and 10-second explicit-wait budgets. Replay
uses one browser process and a fresh offline context per sequence: tests cannot
inherit another sequence's localStorage or cookies, while reloads within a
sequence retain its storage. Larger reports stay in a private, bounded local
file with a hash receipt in the command output. Cale validates that receipt,
the suite identity, user instructions, and current HTML before trusting results;
it does not drop test evidence to fit the log tail. Custom test suites remain
the route for work beyond this bounded browser helper.

The native worker can omit `step_id` when completing, splitting, or revising
the active unit shown in its checkpoint. The inferred ID is bound to the exact
request and acceptance check it saw; SQL split/revise transactions reject newer
work instead of overwriting it. Direct public tool calls still require IDs.
Completing a step runs its saved check and rejects replacement `steps`; changing
acceptance requires `revise`. One-time saved-suite revision references cannot
be stored as repeatable acceptance. Older steps containing those references
must be revised to complete input/assertion cases before they can execute.

Automatic local routing groups eligible aliases by their physical model weights
and chooses the smallest configured window that already fits the request. This
avoids allocating a huge context or retrying identical weights through another
alias for a small step. Larger requests retain eligible larger windows; explicit
manual model choices are respected. This changes memory allocation and retry
selection, not the model's native context limit or the user's input.
- Small single-page HTML repairs temporarily expose core file/edit/check tools, with full discovery restored every fifth repair turn. If a model claims completion without the required browser evidence, Cale can request schema-validated checker-argument JSON instead of relying on a dropped function call. Invalid argument plans receive a bounded correction opportunity; ordinary chat text is never executed. Empty or contradictory visible/hidden assertions are rejected before browser work. A successful check runs only its newly generated verifier automatically; custom verifiers are preserved. Verified numbered plans are accepted alongside checked Markdown milestones. A recovered, verified small HTML task can rerun its owned verifier and finish without another inference call.
- Small-step execution uses an indexed SQLite queue in each project's existing local memory database. Workers are instructed to plan 2–6 small units for the next phase, implement the active unit, and run its saved read-only acceptance command before advancing. A batch has at most eight units. Running the saved command directly, or its equivalent supervisor verifier, also advances the step automatically on a successful check with unchanged project files. Failed checks stay active; oversized units can be split and mistaken check commands revised without erasing completed work. Each call receives the active unit, counts and two recent successes, shorter roadmap excerpts and two recent conversation groups. Routine turns expose a smaller tool set, with specialist discovery restored periodically. New user instructions isolate the old queue, and unfinished units block final handoff. SQLite ships with Python, so there is no database service to install. Step checks are model-chosen tests; their exit codes do not replace independent whole-project, browser or regression checks, or guarantee that a small model can solve every unit.
- Create local 3D draft assets with [TripoSR](https://github.com/VAST-AI-Research/TripoSR) from an image, or have the installed local image maker draw a reference from a prompt. `local.start_3d_job` installs an isolated, pinned Python environment and checksum-verified weights on first use. Its conservative hardware gates require one NVIDIA GPU with 6 GB VRAM, 16 GB RAM and 12 GB free runtime storage. `local.build_3d_scene` builds colored props from boxes, spheres, cylinders and cones on CPU, using [trimesh](https://github.com/mikedh/trimesh). Both paths export one selected GLB, OBJ or STL, reload and validate the mesh, hash the output, and save a software-rendered PNG preview. Agents poll durable progress, inspect previews and cancel exact jobs; registered task Pause/Stop also cancels its 3D worker. The readiness checklist tracks CPU geometry and neural image-to-3D separately. Downloads need internet once; generation uses local inference and requires no account. Assets use vertex colors and need artistic/topology review before production; rigging and UV/PBR texture baking are outside this workflow.
- Qwen 3.8 Flash Next's advertised Ollama package is a 105 GB MLX build for Apple hardware; this Windows x64 installer does not install it. Codestral's current documented FIM service is not added as a free local tool agent. Model identifiers and local compatibility must be verified before adding another runtime.
- If the configured main Ollama alias is missing or proves too slow on this PC, Cale rebuilds that stable alias from an already installed, tool-verified model that meets the hardware and measured-speed limits. It can also move an otherwise working main alias to different weights in the same size tier when the actual installed model is measured at least 50% faster, preserving the configured context window and refreshing the routine alias. It checks the replacement and updates the usable context window before dispatching agent work; it never deletes the only configured main alias without a replacement.
- Automatic avoidance/removal of slow local models uses at least three sustained timing samples from the last 30 days (latest 12 qualified calls). Native decode timing needs at least 64 generated tokens; compatible endpoints without decode timing need at least 128 output tokens. Fast and slow qualifying calls both count. Short replies and older records without timing provenance remain useful for the speed display but cannot trigger removal. End-to-end timing includes prompt processing and model loading, so it measures task throughput rather than pure decoding speed. Hardware and idle-only removal checks still apply.

- Cale records which main-model source tags it downloaded itself. After a verified faster replacement and once tasks are idle, it removes retired source tags it owns; it does not claim or delete models that were already on the PC before setup.
- When a model refuses a request, retry it automatically with an installed derestricted local model. Short requests try a smaller 9B model first; demanding projects try the larger coding model first. Tool-using tasks only retry on locally probed tool-capable models. This also applies to quick chat, file answers, and web answers; if no compatible model answers, Cale shows the original refusal.
- Chat locally on the main screen, or describe work in everyday language to create a persistent task automatically. Use **Add files** beside the chat to ask a quick question about up to three small documents or images; extraction and inference stay on this PC. For long documents, Cale picks passages relevant to the question across the file and labels omitted sections. Requests to edit files, use a folder, or inspect a larger batch become saved tasks. Chat history stays on this PC. Follow-up instructions can be added to an active task in the same chat.
- Calculate explicit arithmetic and percentages exactly in chat, without asking a language model to guess the result. Short expressions support parentheses, thousands separators, division, and powers; unsupported prose stays in ordinary chat.
- Say “I want to build…” or “I need to fix…” to start work directly. Ask for status to see the latest tasks, including work ready for your review; name a task to narrow the answer. The main screen keeps task cards at a readable width on large monitors, puts live voice and setup notes beside Cale's portrait, and shows a subtle Listening, Speaking, Working, or Ready pulse.
- For changing facts, chat searches the public web, reads up to three live pages, and answers with clickable links to the pages it read. On long pages it selects passages relevant to the question across the page and discloses omitted sections. Search results alone are shown only as leads when page reading fails. Business phone lookups check a matching business site before directories when one appears, can read a contact page after the homepage, and compare clickable and visibly printed numbers before answering. They report conflicts or a failed live lookup instead of guessing. Questions about your own phone number, email, address, or hours stay in local chat memory instead of being sent to public business search. Quick chat and source synthesis use installed Ollama models, without spending connected cloud credits.
- Requests entered during initial setup are saved in the task list and start automatically when setup finishes. Large original requests are kept verbatim in the project folder while a bounded preview starts the worker, so the model can read the whole brief in sections without filling its first context window. If several tasks are active, Cale asks which task a follow-up belongs to instead of changing the wrong one.
- Completed app projects stay ready for review without repeatedly reading every project file on each supervisor tick. Cale checks file metadata for quick change detection, repeats the full content hash periodically, and always hashes the complete project again before accepting it.
- Scan website source for generic design patterns with the bundled MIT-licensed `avoid-ai-design` scanner and check text color contrast. For Astro projects, Cale can also add a bundled, offline AI interface component kit with chat, reasoning, attachment, and tool-call UI. It copies files into the project without overwriting edits or requiring a design account. Rendered layouts still need visual review.
- Speak task updates aloud. Switch **Mic On** to listen for “Hey Cale” and a spoken instruction; click again for **Mic Off**. **Ctrl+Alt+Space** while the app is focused records one instruction. A microphone turn or an urgent request to read an earlier reply interrupts Cale's current local voice synthesis or playback; unfinished speech resumes afterward. A spoken clip ends shortly after a pause instead of always waiting the full eight seconds; quiet input still gets the full listening window. The microphone turns on automatically after a device check; speech recognition and playback run locally.
- Generate images, songs, instrumentals, and style covers locally; describe photos and screenshots, transcribe audio, and create offline spoken WAV files. On a capable PC, the separate ACE-Step 1.5 music runtime and model download quietly after launch; other PCs prepare it on the first music request. Music jobs keep their status on disk, recover after a reboot, and reuse the original ACE-Step task when its server is still running. Music generation releases idle Cale LLMs from GPU memory while it renders. Agents can analyze an existing song's estimated tempo, key, chord changes, and peak level locally before editing it; this reads at most three minutes and the harmony estimates can be wrong. Agents can also separate an existing song into vocal and instrumental WAV tracks with Audio Separator, turn a solo instrument recording into editable MIDI notes and a note-event CSV with Spotify Basic Pitch, master a song against a reference song into a new 24-bit WAV with Matchering, or turn isolated vocals into editable lyrics and timed SRT captions using offline Whisper. These install in isolated local runtimes on first use, without an account. Basic Pitch works best with one instrument at a time; reference mastering is best when the reference has the sound and loudness you want. Sung-word recognition can make mistakes, so review the lyrics before publishing.
- A simple song request in chat starts a direct local music task, without making the coding agent plan or browse tools first. Detailed multi-paragraph music briefs beginning "create a ... track" or "create a ... banger" use this route too. An unspecified song or rap defaults to three minutes; a jingle, beat, or soundtrack defaults to 30 seconds. Duration requests such as "1:30" or "1 minute 30 seconds" override those defaults. For a vocal song without supplied words, Cale writes original lyrics before the local music render: Cloud and Free cloud only try a connected cloud model first and fall back to the small local model when the cloud is unavailable; Local and Both use the local model. The words are saved beside the MP3. To use exact words, write a song request followed by `lyrics:` and paste the lines; Cale sends them unchanged to the local music engine. Its My Tasks card follows the durable render. When finished, **Play song** opens the current audio version directly; **Open files** shows every take. A tiny `00-PLAY-THIS.m3u` playlist points to the current completed take. Cale does not create another audio or video file under a second name. Vocal jobs use offline speech recognition to check for sustained words across the song; short vocal hooks must match the sung sections of the lyrics rather than words written under instrumental drops. A playable track with almost no vocals is reported as incomplete. This is a detection check, not proof that every sung word matches the requested lyrics. Ordinary song requests finish with audio and optional lyrics only. A music video is a separate task when someone explicitly asks for one. Pause stops Cale's worker and Continue reattaches to the same ACE-Step task when possible; the local music engine may finish a render already underway. After an interruption, recovery decodes the entire saved recording and checks the requested vocals before marking the task complete. For follow-up requests, Cale can trim a clip, fade it, adjust its volume, or mix spoken audio over a soundtrack with local FFmpeg tools. It can also make custom music visualizers in wide, vertical, or square format, optionally using a supplied cover image behind the animation and reviewed SRT lyrics as on-screen captions. Video rendering is entirely offline with the bundled FFmpeg. Every edit keeps the original. Covers and edits still use the full agent.
- The offline app checker replays fills, button clicks, keyboard input, bounded waits, then visible assertions. Keyboard cases can hold arrow keys or letters and press Space to test keyboard-driven apps and games. For example, `keys:[{key:"ArrowRight",hold_ms:200},{key:"Space"}]` sends real browser events before checking the requested score, status, or other visible result. Saved verifiers replay the same actions after edits. Holds are capped at one second each and total waits at ten seconds per check.
- The native worker can open a local HTML app with `browser_open_project` and a relative path, without installing or starting a web server. Its separate offline browser profile permits only project files and inline resources. Browser snapshots, clicks, typing, and the side preview then work on that local page; opening a normal website switches back to the task's online profile.
- Songs requesting sparse vocal chops use them as sound-design cues. They do not generate full verses or demand sustained singing in the completion check. Rap, lead singing, and supplied lyrics still get the full vocal check. Listen to judge whether the requested sound is present.
- A simple image request also becomes a durable My Tasks card. The local image worker can recover after the app closes or Windows restarts, and Cale checks the PNG's pixels before marking it ready. Pause stops the render and Continue restarts it; detailed image edits still go to the full agent.
- General tasks have a private local Chromium browser with page snapshots and a side preview. It can fill fields, select dropdowns, press common keys, follow links, go back, and save clicked downloads into that task's `Downloads` folder. A browser download is limited to 512 MB; larger files need a project command. Browser downloads use numbered controls from the current snapshot and never overwrite an existing task file.
- The native worker can inspect a downloaded or project PDF, Word document, spreadsheet, presentation, image, or text file without sending it to a cloud extraction service. It returns bounded excerpts, supports follow-up questions or character offsets, and caches extracted text by the source file's hash inside the task folder. It rejects files outside the task folder and files above 50 MB.
- Selected PDFs can mix selectable text and scanned pages: Cale retains the native text and uses the local vision model on scan-only pages. Quick chat samples up to 6 scanned pages, file memory up to 12, and the agent's document tool up to 20, spread through longer PDFs. Cale labels vision transcriptions as unverified and identifies omitted scan pages; check the original for important details. No document is sent to a cloud OCR service.
- When you select files or folders, Cale builds a private local search index and verifies matching sources before showing them as current. Later questions about those files require the named terms to match, so one invoice does not stand in for another. A search with no indexed match also scans selected folders to discover files added later, even when the UI background scanner has stopped. Secret-named files and nested secret or credential folders stay out of this searchable memory, including entries indexed by older versions.
- Inspect sampled video frames and optionally transcribe a video's audio. Agents can capture a camera frame or watch up to four snapshots over 30 seconds only after you enable Camera in the app; that switch expires after one hour.
- Edit local videos without an account: trim, change speed, reframe for wide/vertical/square, join clips, and replace a soundtrack. Cale uses the bundled FFmpeg runtime and saves a new MP4 while preserving the source files.
- Generate short local video clips from a prompt or starting image with Wan 2.2 5B through an isolated ComfyUI runtime. Cale installs the runtime and checksum-verified model files on first use when the PC has an NVIDIA GPU with at least 10 GB VRAM and 35 GB free disk. Later renders need 5 GB free on the runtime drive and 1 GB on the output drive. Concurrent jobs wait for a verified runtime instead of opening partly downloaded weights, then take turns on the GPU so two large renders do not compete for memory. Job status reports sampler progress during rendering. Clip generation may be slow, and longer videos can be assembled from multiple clips. This feature does not use a cloud account.
- Discover no-key public APIs for projects from a daily catalog, with a local cache for later searches. Cale treats results as leads and checks each provider's own documentation before using one. Connected free cloud AI remains optional; OpenRouter's free-model router is already supported.
- Find reviewed public training datasets and download their pinned GitHub files into a task when needed. The initial catalogue contains [CLINC150](https://github.com/clinc/oos-eval) for intent/out-of-scope evaluation. Downloads verify exact data and license hashes, retain attribution, preserve existing files, and reuse a verified cache offline. This supplies data; it does not execute downloaded code, train a model, or establish that the benchmark matches a user's task.
- Train a small text classifier from labeled CSV rows or a separate 0.5B LoRA adapter from JSONL examples, with held-out evaluations saved beside the result. For spreadsheet-style CSV data, Cale can also train a local classifier or number predictor with scikit-learn, compare it to a baseline on the same holdout rows, save safe JSON weights, and add predictions to new CSV rows. Repeated numeric codes can be recognized as category labels automatically. Batched sparse prediction keeps wide datasets practical, preserves an existing `prediction` column, and carries the holdout quality warning forward when the model is reused. It does not change the main coding model.

The agent's terminal can modify files with your Windows permissions. Choose a specific project folder and keep normal backups. It asks for review; passing tests does not prove an app is correct or production-ready.

Image generation uses Segmind SSD-1B when the PC has at least 8 GB of GPU memory and enough free disk space; it downloads automatically in the background after an update. Smaller PCs keep LCM Dreamshaper v7. Speech recognition uses Faster-Whisper base; the training base is Qwen2.5-0.5B-Instruct. The local image generator and voice tools are separate from the main coding model. Camera capture is off by default. When a task requests camera use, its card offers a one-hour permission button. The **Camera On** button shows a small live webcam tile next to Cale's animated beach portrait and watches for visitors for one hour, then turns itself off. The tile uses frames from the same local camera worker as greeting detection, without opening the device twice or saving frames. It uses local YuNet and SFace models to learn faces only after someone tells Cale their name with the microphone; saved face embeddings stay on the PC, and camera frames are not recorded. Recognition can be wrong. The camera tools also find ONVIF devices on the same network and accept an owner-provided RTSP or HTTP stream address for snapshots, watching, or greetings. Cameras that do not support ONVIF or require a proprietary app may need manual setup. Video analysis samples up to eight frames, so it can miss activity between them. Camera learning does not alter the main coding model.

## Long tasks

Pure JavaScript logic can use `check_javascript_function` with a project path,
function name, positional JSON `args`, and an `expected` value or exception
constructor name in `raises`. The same typed cases work in SQLite step
acceptance checks. This runs current source through the bundled Node runtime;
plain scripts, CommonJS exports, and import-free ES modules (`.js`, `.cjs`,
`.mjs`) are supported. Each case starts with fresh module state. Functions must
be synchronous and return finite, plain JSON values. DOM interactions, imports,
and asynchronous workflows need the project's browser or integration tests.

Cases accumulate across up to eight functions, with 24 cases per function.
The generated verifier binds the saved suite to its checksum and current user
instructions, detects source changes during verification, and reruns all retained
cases. Existing custom or other component verifiers are preserved; a passing
function check does not replace an integration check. Nested argument errors
identify the incorrect field, and failures show the actual function signature
to help repair positional-input mistakes. Large integer comparisons no longer
round unequal integers into a passing result. Context compaction also preserves
a short durable plan when it fits alongside the tool catalog.

When a saved Python or JavaScript input is itself mistaken, the function checker
can replace that single case using its one-based `saved_case`, current
`suite_sha256`, exact path/function, and `cases=[one corrected case]`. All other
cases remain and the suite reruns. Stale checksums and old-request references
are rejected. This is a one-time repair action; SQLite steps store the corrected
repeatable acceptance arguments, not a revision reference. Correcting a saved
case alone does not complete an active step or prove the revised test covers
the user's requirements.
The native worker's generic file tools preserve these generated suite files and
direct the model to the checker API for revisions instead of raw JSON edits.
Passing a component verifier while SQLite steps remain no longer triggers the
worker's overall-plan completion reminder. The worker must continue the queue;
project handoff still requires all steps and suitable whole-project checks.

After work begins, the native runner can check saved Python, JavaScript and
offline browser acceptance automatically before the next inference request.
It uses the same checker and SQLite commit path as `task_steps complete`, advances
at most eight units per pass, and stops at a failed check or missing source.
An unchanged failure is not automatically repeated across worker restarts;
source, acceptance, request or retained-suite changes allow another attempt.
Pause/Stop, task removal, new instructions, acceptance revisions and source
changes during checking prevent stale progress from being committed. Automatic
browser checks retain the input-effect and page-evidence requirements. Shell and
custom integration commands remain explicit tool actions. These checks reduce
bookkeeping inference; they do not create acceptance cases or prove test coverage
of requirements the model omitted.

For implementation requests that clearly name one Python/JavaScript module and
declare two to eight plain function signatures on separate lines, the briefing
now lists the required APIs and which have typed cases in that actual module.
A generated function verifier cannot hand off while one is missing or its suite
checksum is stale. Checks in a test-only copy do not cover the requested module.
This is name coverage, not proof of correct behavior or edge-case coverage.
Ambiguous multi-module requests and custom integration verifiers retain their
existing workflow instead of being forced into this narrow function format.

Later explicit API scope changes can be recorded through `task_steps scope`
using `scope_operation='exclude'` or `'only'`, `function_names`, and an exact
`instruction_quote` from the latest user-follow-up entry. The quote must name
the selected APIs and express a scope change; a plan or progress claim cannot
waive coverage. The SQLite record retains its source provenance across unrelated
follow-ups. Edited provenance or later discussion of omitted APIs requires
scope to be reconciled again. This records the agent's interpretation of an
explicit instruction; it is not a general natural-language intent proof.

The current SQLite acceptance is placed ahead of older prose in each fresh
briefing. When its source file is at most 6,000 bytes, the worker also gets those
current bytes and their digest without a separate read call. Larger sources
continue through the normal bounded file tools. A valid, once-double-encoded
native step list can be decoded and schema-validated without retrying inference;
malformed lists and invalid step contents remain errors. Browser diagnostics
also distinguish literal text assertions from presence checks for canvas/image
elements; an invented `'exists'` expectation still fails rather than changing
the page or silently weakening the check.

The model still chooses its tests. The retained local JavaScript repair trial
failed to complete its four steps, despite partial code repairs. Passing the
checker implementation's regression suite is not proof of reliable game
building; see the developer benchmark evidence below.

The project queue and native Hermes task board use SQLite. Each project also gets `.local-ai/memory.sqlite3` automatically, using the SQLite FTS5 engine bundled with Python. The agent can search indexed files and save typed memory; it re-reads live files before trusting an excerpt. The worker maintains `.local-ai/PLAN.md` and `PROGRESS.md`. When Hermes exhausts one worker's goal-turn budget, the supervisor waits for that run to exit, then starts a new generation from the saved files and user follow-ups. Actual requests for user input still stop for a decision. The supervisor runs the project's verification script independently, starts a separate review pass, and checks again before acceptance. It hashes project file contents before and after verification, so a check that edits the project cannot certify its own new output. It also compares file contents before acceptance, including same-size edits with unchanged timestamps. Existing tasks waiting for review get a fresh independent check when upgraded to this stronger record. Repeated verification failures stop for input rather than retrying forever.

Your PC must be on and awake. The optional sign-in task restarts the supervisor when you sign back in. This is resumable work across days, not a promise that any request will complete unattended. One demanding local worker runs at a time.

The background supervisor records task milestones even when the window is closed, so the estimated time to review reflects ongoing work. Pauses and time while the supervisor is stopped do not count.

Each task card shows the upstream AI providers and their reported token totals. The loopback address used by a worker is Cale's router, so a local address alone does not mean Ollama answered. Open a task's details and choose **Use local AI only** to keep that task on installed Ollama models; this takes effect on the next model request, even while the task is running. **Use connected AI** restores the normal speed and usage routing. Audio rendering remains local in either mode.

Standalone song tasks now start with local-only planning as well as local rendering. Someone can opt that task into connected AI from its details. A detailed music brief beginning with tempo or style is treated as a task even without "make a song"; the chat answer path cannot claim it created a task when no task was saved.

Song task details include **Listen to saved versions** so all playable takes remain available without searching through diagnostic clips. Cale's worker instructions count existing full-song drafts across restarts and limit each user-requested batch to three new versions; after that, Cale should present the best saved drafts and ask for direction rather than silently making more.

After a standalone song passes its independent project checks and has a saved playable file, its task finishes without a second open-ended AI review. You can listen to the saved takes and offer feedback later. Older song reviews stalled only on which version sounds best can also finish after fresh checks; a failed check still returns to repair.

The desktop rechecks GitHub releases while long tasks are running. If a newer release appears before a task finishes, Cale stages the newest version and applies it when the task is idle. The first-run AI install holds tasks until the core runtime is ready; later updates to optional browser tools run in the background without stopping existing or newly submitted tasks. Setup repair checks which installer owns the progress record, so a quiet optional-tool download cannot trigger a second base install.

Automatic model sizing ignores Hermes's own board-finish reminders. A simple task therefore stays on the appropriate fast or middle model after a reminder, while a real new user request can still scale it up.

Updates wait for the actual Hermes worker process to exit, including the short retirement period after Pause or Stop changes a task card's status.

When a finished image or song gains a file after review, Cale automatically rechecks the saved media and gives it a fresh review fingerprint. A file change alone no longer sends that task to "Needs your help"; an invalid result still does. Existing media tasks stranded by the older changed-files message recover through the same check after updating.

Finished task cards show a full 100% gauge and a frozen total time, with a simple Finished label instead of rating buttons. Open files, reuse the prompt, or delete the task from the card. Ask Cale in chat to change a finished result; the saved task can reopen for a revision. Website and app tasks whose files change after review now start a new independent review generation automatically, preserving user edits and still requiring a fresh look before any acceptance.

Thumbs up accepts a reviewed task in one click. Thumbs down asks what to improve and starts a revision. Cale keeps explicit reviews in a local SQLite store, retrieves relevant notes for later work, and lets the owner remove a learned note from the task detail view. This is similarity-based preference learning, not model-weight training. Local music requests for Katy Perry-style pop add a female lead-vocal cue; techno and related dance genres put drum-machine and synthesizer cues first. Phrases such as "without guitars" or "not a guy voice" are translated into positive music directions so the rejected sound is not repeated in the model caption. Feedback about an unwanted singer or guitar-led techno can start a new render while preserving the earlier file. These directions improve the odds of a match but do not verify the singer or instruments in the resulting audio; listen before using it.

When someone asks Cale to revise a finished image or song, the task keeps its original plan and adds fresh revision milestones. Its progress starts over for the new request. A simple **add vocals** follow-up starts a direct local job that creates a newly named vocal version beside the original audio. It uses the original theme and duration, then checks the rendered vocals; it does not claim to add vocals to the exact same backing track. Requests to keep or edit the exact backing still use the full agent workflow. An empty revision message leaves the existing render running. If a new vocal song renders mostly instrumental audio, Cale preserves that file and automatically tries again with its saved lyrics and a clearer vocal direction, up to two retries before asking for help.

The supervisor handles task controls even when a local model or gateway is temporarily unavailable. A failure on one task is recorded for retry without preventing other saved tasks from advancing. Independent review workers start with a strong planning pass, including when the original task was a short song request. Repeated recent tool failures make Cale use a stronger reasoning model for the next step; successful tool work returns to normal automatic sizing. To avoid expensive model reloads, Cale reuses a suitable model already in memory and prepares a 64K-context main-model alias for routine requests while keeping the full window available for long ones. If first-run setup stops unexpectedly, Cale detects the stopped installer and retries automatically with backoff; a still-running download is never interrupted just because it is slow. Launching the desktop again brings the existing window forward, and it checks for updates every five minutes while open.
The model router sizes each worker step using the original project and recent user follow-ups, so adding a substantial requirement can move the work to a stronger installed model even when the original request was simple.
If independent verification fails after a review handoff, Cale creates a fresh worker card with the saved plan and failure output. User requests for changes after review also start a fresh generation. This avoids relying on Hermes reopening a goal card that its judge may reject.

## Free cloud connection

The header offers **Local**, **Free cloud only**, **Cloud**, and **Both**. A task explicitly set to **Local only** stays local regardless of the header mode, including when its local model fails; it never falls back to cloud. Other tasks follow the header mode. Free cloud only sends chat and task reasoning to connected free-plan providers, never the enabled ChatGPT plan or a local chat model. Local file search, browser actions, image, music, video, and other tools still run on the PC; a music tool may use the small local model to write lyrics if cloud is unavailable. If free accounts are exhausted or disconnected, Cale says so instead of silently spending paid tokens. Provider-side billing rules can change, so the provider dashboard remains the final source for account charges.

When setup finishes, the app opens an OpenRouter browser connection automatically. Sign in and approve the connection once; the app saves the resulting key privately for that Windows account. You never need to copy an API key. The rocket button at the top opens **Supercharge my AI**, where you can reconnect OpenRouter or connect ChatGPT. Connecting allows task prompts and tool results to be sent to the selected online provider; an in-flight request may finish after disconnecting.

ChatGPT connection uses OpenAI's public-client Sign in with ChatGPT flow. It verifies the returned identity and stores OAuth tokens with Windows DPAPI. ChatGPT plan use stays off until you explicitly enable it. When enabled, deep requests use GPT-6.1 Sol when available and balanced requests use the lighter GPT-6 Luna when local generation is slow. After repeated successful plan calls without a limit, Cale uses the plan more freely for balanced work; a plan limit shifts balanced work to free cloud or local models while preserving paid access for deep work after cooldown. Quick requests stay local. Each free provider has its own limit history, so exhausting one does not hide another. Repeated free limits reserve that provider for deep or especially slow local work. This learning stores only provider, task size, outcome, and time on the PC; it does not know the account's actual remaining quota. Plan requests draw from the same ChatGPT/Codex allowance as other connected apps, not from a separate free pool. ChatGPT plan use is subject to account eligibility and the [current OpenAI preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations). Review or limit usage in [ChatGPT settings](https://chatgpt.com/settings/usage).

The router uses `openrouter/free`, forces zero maximum provider prices, and strips alternative model routes and plugins. OpenRouter's free quotas and availability are limited and can change. On demanding requests it tries eligible direct free-plan connections first, then can use OpenRouter before local inference.

The same screen can connect a personal Google AI Studio, Groq, NVIDIA NIM, Mistral AI, SambaNova Cloud, Zhipu AI (BigModel), ModelScope, Aion Labs, Merkium, Cohere, Hugging Face, or Cloudflare Workers AI account. These services use their own API keys; the button opens the provider's key page and the key is stored with Windows DPAPI. Cloudflare also requires the account's 32-character ID. Only the account owner can confirm that their key is on a free plan or using free credits. A key from a paid workspace may incur charges, and provider quotas and model access change over time. Cale uses conservative request budgets, uses recent measured provider speeds when available and chooses a cloud route when its expected response time is lower, and falls back to local models on an error or quota limit. [Merkium documents](https://www.merkium.nl/docs/pricing) a permanently free, no-card API for Ganymede 1.1 with tool calls; Cale caps it at 5 requests per minute and 40 per day. Its published rate limit is one request every two seconds, and no daily limit is published. [Cerebras now offers only one-time trial credit](https://www.cerebras.ai/pricing), so Cale stops routing to it even if an old key remains saved; reconnecting it as a free provider is disabled. Hugging Face free accounts include a small monthly inference credit; Cale limits that route to two short calls per month, which is a usage safeguard rather than a billing guarantee. Cloudflare's [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) includes 10,000 free Neurons per day; Cale reserves at most 8,500 estimated Neurons daily for its Workers AI route to leave a margin. Other Workers AI use on the same account is outside that local budget. ModelScope provides 200 free Magicubes daily to registered users; the selected Qwen model currently costs one Magicube per API call, and Cale caps its own use at 150 requests per day. Aion Labs has a default free tier with a 20,000-token daily limit; Cale reserves at most 10,000 estimated tokens per day on its Aion route, including the maximum possible output. Its free daily credit amount is set by Aion, so actual account access remains authoritative. Cohere uses Command A+, which Cohere currently lists as free until its rate limits. NVIDIA offers free hosted endpoints for prototyping through its Developer Program, not unrestricted production use. The **Go offline** switch prevents these cloud calls. Google says free-tier content may be used to improve its products; keep private documents local if that is not acceptable. GitHub Models is not offered as an inference route because GitHub retired that API in July 2026.

Keys you add yourself are encrypted with Windows DPAPI; they are not written into the Hermes configuration or routing logs.

No account keys are included in the repository or installer. Local models work without a cloud service.

Each task card shows the tokens reported by text models used for that task. Music cards identify local ACE-Step audio separately because its generation does not report a token count. Counts begin when this feature is installed and exclude providers that omit usage data. The lifetime graph retains its separate all-task total.

See [OpenRouter's free model information](https://openrouter.ai/collections/free-models/) and [account connection documentation](https://openrouter.ai/docs/guides/overview/auth/oauth).

## Models included in the project

The repository contains model definitions, sources, configuration, and automatic download code. The installer places the actual weights in Ollama's local model store. It does not put tens of gigabytes into Git history or require users to find model files themselves.
Source updates read the installed model name from the user's existing configuration, so earlier installs using a different local alias keep working without manual edits.

| Model | Approximate download | Suggested RAM | Role |
|---|---:|---:|---|
| Qwen3-Coder-Next Q4 | 52 GB | 96 GB | Main coding model on large PCs |
| Qwen 3.5 9B | 7 GB | 24 GB | Smaller default |
| Qwen 3.5 4B | 4 GB | 16 GB | Light default |
| Qwen 3.5 35B | 22 GB | 48 GB | Balanced optional local model with tool support |
| [Huihui Qwen 3.5 9B abliterated](https://ollama.com/huihui_ai/qwen3.5-abliterated:9b) | 6.6 GB | 16 GB | Automatic small derestricted fallback after a local tool-call probe |
| Huihui Qwen Coder Next abliterated | 52 GB | 96 GB | Optional local coding variant |
| GPT-OSS 20B (official Ollama package) | 14 GB | 32 GB | Tool-ready optional local model |
| ArliAI GPT-OSS 20B Derestricted | 13 GB | 32 GB | Manual experimental variant; its tested GGUF did not advertise tool support in Ollama |
| Huihui Llama 3.3 70B abliterated | 43 GB | 80 GB | Experimental optional model |
| ArliAI GLM 4.5 Air Derestricted Q2 | 45 GB | 80 GB | Manual experimental model; tool support unverified |

RAM guidance is conservative, not a benchmark. Speed depends heavily on GPU memory, CPU and context size. Setup downloads the recommended base model, vision model, image, speech, and training weights automatically. After setup, the app automatically downloads tool-ready models that fit physical RAM and leave at least 12 GB of storage headroom. Model downloads run in the background and stay off the main task screen. The ArliAI GPT-OSS GGUF remains a manual experiment because it did not advertise tool support in our Ollama test; the official [GPT-OSS package](https://ollama.com/library/gpt-oss) does. The app checks each installed model's advertised tool support before routing tasks to it. “Abliterated” or “derestricted” describes the publisher's modifications; it does not establish better reasoning, reliability, or suitability for a particular task. See [model sources and licenses](THIRD-PARTY-NOTICES.md).

## Development

The companion source is MIT licensed. Third-party runtimes, model weights and dependencies keep their own licenses.

Start with [the architecture guide](ARCHITECTURE.md) and [contribution guide](CONTRIBUTING.md) for a map of the code and its checks. The primary files are `long_tasks.py`, `cloud_ai.py`, `chatgpt_auth.py`, `toolbox_mcp.py`, `model_library.py`, `web_tools.py`, `media_worker.py`, and `small_training.py`. `config-template.yaml` contains no account secrets. `Install-Backend.ps1` performs the same setup used by the graphical installer. The app displays the [Cale Media logo](ASSETS.md) in its header and installer.

After installation, run `Toolbox/python/Scripts/python.exe Validation/test_cloud_ai.py` and `Toolbox/python/Scripts/python.exe Validation/test_long_tasks.py` from the installation directory. Tests cover real process termination, recovery state, stale verification, OAuth callback validation, free-only routing and fallback. They do not require a cloud account.

For real local-model tasks rather than mocked component tests, run the opt-in [native worker benchmark](benchmarks/README.md). Its separate graders check a coding repair, a CSV cleanup, an interactive HTML app, and a two-page PDF with superseded draft terms. They also challenge each saved verifier with a regression. These small tasks are not an overall comparison with other agents.

Please report reproducible bugs and include versions and redacted errors. Do not upload your `Cloud`, `data`, `Projects`, `LongTasks`, or log directories.

Made by [Cale Media — websites and digital marketing](https://calemedia.com/).

New songs include a square MP4 using the supplied DJ Cale concert artwork. Audio remains a separate playable file; the video reuses it without regenerating music. **Share to Facebook** on finished song cards prepares/reuses the video, copies its path, reveals it in Explorer and opens Facebook. Choose Photo/video, select the MP4, then choose your audience and publish. The app does not publish automatically or require a Facebook API key.

Local HTML space-shooter tasks can reuse an original bundled game engine through `create_game_starter`, then customize it. Cale saves two real SQLite acceptance units and checks actual keyboard input, mobile layout and 18 default gameplay behaviors in Chromium. A source-bound library verifier can advance automatically; arbitrary project scripts remain under the normal tool workflow. An unchanged baseline can receive a bounded local requirements review; invalid or missing-feature reviews return to normal work. From-scratch requests, existing HTML projects and other engines are excluded from automatic starter selection. These are starter-assisted builds, not proof of broad model coding ability.

Bounded starter selection/review uses the quick tier while preserving explicit model and local/cloud choices. Ollama models that advertise thinking support can disable it for those short calls. These calls have a 90-second local timeout and do not retry aliases over identical weights. [Ollama reasoning compatibility](https://docs.ollama.com/api/openai-compatibility) documents the thinking-control mapping.

Starter-derived games now bind the supported starting-life setting to the original request and later user instructions, including simple number words. Damage rules such as “a hit removes one life” are not starting counts. Saved version-2 component verifiers test the requested count, reset, damage, shield expiry and HUD; version-1 specs remain readable. A separate source/request-bound browser completion check rejects a wrong starting/reset count even when prose claims success or a stale default verifier passed. Direct starter-tool calls also register this gate. This is a check for a supported setting, not a general natural-language requirements verifier.

The bounded game requirements review uses one structured function call and checks exact requirement quotes. It can review a correctly verified starting-life customization while preserving an already-completed custom plan. Missing features, invalid reviews and stale checks return to normal work. Local thinking-capable models that return no answer or tool call can receive one validated retry with thinking disabled; reported token usage for both calls remains recorded. A failed recovery does not retry a larger-context alias over identical weights.

Branded song-video completion does not require a captions file. Legacy captioned exports still require their SRT, and any explicitly declared captions must exist. Cards blocked by the old caption error automatically recheck the already-completed media and finish without generating another song or video.

Branded videos on song task cards now include source-audio and video checksums. If an export disappears or its bytes change, Cale queues a video-only repair from the saved song. The worker pins the audio checksum, skips music generation, lyrics and transcription, and returns the card to Finished when the export succeeds. Duplicate polls share a per-job lock; Stop/changed audio prevent repair setup. Persistent failures are bounded to three automatic repairs, and Retry repeats the video stage on the same job instead of creating another song. Existing damaged exports are preserved, and subsequent Facebook sharing chooses a verified export. Legacy captioned formats keep their required captions.


### Accepted donation credit

The leaderboard counts each verified donation PR once, using its GitHub numeric author ID. A normal merge counts automatically. When a maintainer integrates reviewed code directly on `main` and closes the original PR, the maintainer applies the upstream `cale-accepted-fix` label after verifying its integration receipt. Closing a PR, editing its body, or claiming token totals does not grant credit. Donors do not label their own submissions on the upstream repository.

Removing that acceptance label withdraws manually integrated credit on the next complete refresh. Partial or failed scans preserve the last verified leaderboard, and renamed accounts retain credit through their stable GitHub identity. The 24 recorded manual integrations in v0.5.486 have been backfilled and verified against live GitHub data. Maintainers apply the same label to future accepted integrations, including closed drafts.


### Small CPU classifier candidates

`find_training_datasets` and `acquire_training_dataset` can acquire the reviewed, commit/hash-pinned CLINC150 public intent benchmark with its attribution and license. Acquisition retains raw files and prepares split-tagged CSVs. Official test and out-of-scope test rows stay unchanged; exact casefold/whitespace text overlaps are removed from training/validation, with exact filter counts and hashes recorded in `normalization.json`. Training also excludes original validation texts. This prevents exact-text leakage, not paraphrase overlap.

`train_text_classifier` uses the existing local TF-IDF/logistic trainer. Split-tagged data fits **only train**; validation and test are evaluation-only. An optional exact label subset filters all partitions before fitting and records its policy/hash; omitted intents are not silently relabelled as OOS. Plain CSVs retain the stratified holdout option. Models save portable JSON weights, held-out metrics, majority/OOS baselines where applicable, CPU prediction latency and a hash-bound **candidate** registry. No score, training loss or benchmark result automatically activates routing. CLINC predicts its published intents; it does not establish Cale tool-use skill or suitability for user tasks.

`classify_text` runs portable CPU inference on a thread, without starting the media runtime or importing sklearn/numpy. A bounded two-model cache avoids rebuilding the model on every call. Prediction preserves the fitted TF-IDF and binary/multiclass logistic probabilities and reports the artifact hash actually loaded. LoRA specialists and agentic training remain separate workflows; this classifier does not replace or fine-tune the main assistant.


### Durable capability recovery

The native worker can persist a bounded recovery episode when an actual tool/dependency is missing, a call repeatedly fails, unchanged reads stall, or a live inability claim conflicts with an existing unfinished acceptance/artifact. Quoted stories, ordinary questions without an explicit artifact/acceptance, and provider-policy/account barriers are not generic permission to download or train anything.

Recovery first searches existing local capabilities without launching/installing backends. The worker then uses its normal authorized tool loop to reuse a tool or build/test a small local helper. Relevant GitHub source review and learning workflows are later options when appropriate; downloading a repository is not permission to run it, and the reviewed CLINC intent dataset cannot solve arbitrary software problems. Account, offline, hardware, spending and task-control rules stay in force.

Episodes and attempted method hashes live in the project's existing SQLite database. Six reserved recovery turns allow at most four executed scoped tool calls each (24 total); a separate 24-entry distinct-method receipt cap survives restarts. Automatic host acceptance checks retain their existing limits. Changing a check does not reset the recovery budgets. Fresh failure of the same recovered checker reopens its existing family without resetting discovery or budgets. A helper test, successful process/HTTP request, model narrative or SQLite done row is not recovery proof. Only the original exact host acceptance observation, with unchanged source and goal identities, earns recovery; another unit/goal supersedes old linkage without pretending it passed. The original task's completion gates remain independent. When the recovery stage ends, the unfinished task continues with its checkpoint/method history rather than being marked done or requiring a new human prompt by default. No-unit coverage is deliberately narrow: only an exact original structured blocked checker can prove recovery automatically.


## Scoped host verification evidence

Actual host checker observations are cached in the project's existing SQLite memory database with source, original instructions, suite and checker fingerprints. Structured acceptance and a successful command process are labelled separately; commands alone do not prove behavior. Unbound CLI observations remain unbound until an authoritative task context is available. These receipts are not training approval and do not replace the actual verifier.

Content snapshots stream every included byte in chunks of at most 1 MiB, with no stat-only digest cache. A capture is bounded to 2 GiB total, 16,384 files, 65,536 visited entries, 4 MiB of relative-path metadata and 60 seconds. The byte/count ceilings can represent a1.23 GB /12,566-file import without buffering its assets in RAM, but this does not guarantee it completes inside the elapsed bound. Actual qualification on a1.23 GB game first reached60 seconds before finishing and correctly remained limited; a subsequent warm-cache capture completed12,569 files/1,230,233,605 bytes in15.646 seconds. Support therefore depends materially on disk/cache latency, and every capture still streams all included bytes. Each capture rehashes content, checks path and opened-file identity before/after (including device, inode and ctime within each API), and rechecks directory listings. Windows path stat and descriptor stat expose different ctime semantics, so their bridge compares device/inode/size/mtime while each API retains its own ctime comparison. Large captures can take seconds or reach the elapsed limit on slower disks; checker-before/after and later freshness checks each incur this real disk cost. Changed/unavailable files, directory races and exhausted budgets yield explicit limited reasons, never a full-source claim. Omitted media, links or dependency limits are explicitly marked limited. Intentional excluded runtime roots are named in the manifest digest (at most128 roots/8 KiB); only an otherwise complete scan is labelled `application_source`, never `full_project_source`. That narrower scope satisfies the necessary reload witness only alongside the same fresh real report’s complete `project_local_dependencies` observation. Loading any excluded runtime asset still yields limited browser dependency scope and blocks this witness. Project metadata/task-state/cache directories are excluded from source snapshots; if the browser serves an asset from those directories, dependency scope is limited too. Such partial evidence cannot satisfy the full-source reload guard. The separate browser dependency-count bound remains unchanged here; dependency-heavy applications can still exceed it. Excluded runtime contents are never certified, while unused build dependencies no longer prevent a complete application-source witness. Only supported, fresh evidence earns that narrow witness; these records do not prove every natural-language requirement, general arithmetic correctness, or model-training suitability. No semantic review model or new inference call is added.


Reload evidence also recognizes an explicit imperative such as “Reloading must preserve added records and deletions.” Added and removed data need distinct measured effects in one page sequence; additions alone do not cover deletions. A removal needs an earlier successful reload baseline, then an observed native table/UL/OL collection decrease (or exported array.length state), then reload retaining the exact new outcome. Returning to the initial empty/zero without that persisted baseline does not prove storage. Click/key-only changes need a baseline too. These remain necessary partial observations, not proof of every field or semantic requirement.

A host receipt additionally consumes a one-use in-process execution permit bound to the exact actual report and arguments. Only the loaded owned checker function's code, module globals and matching source can issue it, including normal script __main__ entrypoints. Ordinary imported record/issuer calls cannot mint a browser pass; report substitution and replay are rejected. This does not prevent a same-user attacker from deliberately monkeypatching the process or editing the database. Unavailable/aborted external dependencies always leave limited scope even when inline fallback behavior passes. Receipt capture never changes the actual check's verdict.


Collection observations capture pristine DOM descriptors before the app starts, including table/section rows, list children, collection lengths/items and query/tag helpers. Later instance or prototype overrides are not called. Global witness selection reserves complete creation/removal pairs for the same collection and sequence before generic evidence, within 32 items and a 24 KB metadata budget; all actual verdicts, case counts and saved sequence numbers remain authoritative.


### Project-local expression helpers

Cale can register and discover small reusable JSON/arithmetic helpers in immutable project-local records under `.local-ai/expression-helpers/v1/records`. Discovery builds a bounded in-process metadata index; ordinary task progress remains in SQLite. These are restricted expressions, not Python scripts or trained models: imports, attributes, shell, network and filesystem effects are unavailable. Registration runs 1–24 supplied examples; passing them establishes only those examples, not general correctness. Definitions, examples, the loaded interpreter and the full original instructions are hash-bound and rechecked before reuse. Helpers stay in their own registered task; they are not shared globally or donated as training data.

A saved `check_expression_helper` unit can run through the ordinary worker without an inference call, compare its real output with the unchanged saved expectations, and checkpoint at the next unfinished unit. This does not complete the whole project or replace its independent verifier. Changed instructions or interpreter code require revalidation. The interpreter is portable Python; registry operations currently require Windows ownership handles and atomic publication without replacing existing files. Records are limited to 128 entries, with a separate 128-entry staging limit; unknown or interrupted staging files are preserved. This is ordinary project containment, not a sandbox against deliberate same-user process or database tampering. No new packages, models or accounts are needed.
