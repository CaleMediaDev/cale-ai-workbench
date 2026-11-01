# How Cale AI Workbench is put together

This guide is for someone reading the code for the first time. The app has four cooperating parts: a Windows installer, a desktop task list, a persistent task supervisor, and local/cloud model services. They run as separate processes so closing the window does not stop a task or a model download.

```text
Setup.ps1 → Install-Backend.ps1 → installed runtimes, tools, models
                                      ↓
long_tasks.py (window) → projects.db ← long_tasks.py --monitor
       ↓                                      ↓
model_library.py --auto                 Hermes task board/worker
       ↓                                      ↓
Ollama model store                  cloud_ai.py → ChatGPT (opt-in)
                                           ├→ OpenRouter free
                                           └→ local Ollama
```

## Where to start

| File | Responsibility |
| --- | --- |
| `Setup.ps1` | Simple Windows installer window. Starts setup automatically and shows the log. |
| `Install-Backend.ps1` | Checks hardware and disk space; installs pinned runtimes and tools; downloads the required models; starts optional model checks. |
| `long_tasks.py` | Stores human tasks in SQLite, supervises the Hermes board, runs independent verification, and draws the desktop UI. `gui()` is the window entry point; `monitor()` is the background entry point. |
| `project_memory.py` | Creates each project's private SQLite FTS5 database, indexes project files, and stores typed requirements, decisions, lessons, components, and skill recipes. |
| `site_crawler.py` | Runs bounded same-host public website crawls with the local Crawl4AI browser, saves Markdown under each project, and indexes each saved page for fast memory search. |
| `chrome_connector.py`, `vendor/hermes-connector/` | Prepare the pinned local Hermes Connector companion and provide a private pairing code to the user through the desktop UI. Chrome's extension and tab attachment remain user-controlled. |
| `usage_history.py` | Stores provider-reported token counts and measured response speed in a private local SQLite database and supplies daily/monthly totals for the desktop chart. Earlier model calls without usage telemetry are not estimated. |
| `runtime_features.py` | Adds the pinned crawler browser and Chrome companion in the background after an in-app update, showing progress on the task screen. Fresh installations prepare them during setup. |
| `project_preview.py`, `preview_capture.js` | Select local task previews and capture web pages into a side window; Chrome DevTools actions also mirror their selected page through `toolbox_mcp.py`. |
| `model_library.py` | Defines optional Ollama models, checks RAM and disk space, downloads sequentially, and writes private progress JSON. |
| `auto_route.py`, `cloud_ai.py` | Sizes each request without an extra AI call, chooses an installed tool-capable local model, and handles explicit ChatGPT plan use plus free OpenRouter fallback. |
| `model_identity.py` | Reads the installed Hermes model name so source updates preserve older working Ollama aliases. |
| `voice_assistant.py`, `natural_voice.py`, `cale_voice.py`, `cale_voice_worker.py`, `avatar_video.py` | Run offline speech, install Kokoro first, then warm the optional local Qwen3-TTS voice clone on capable GPUs; keep its worker loaded between utterances and show an illustrated beach character during speech. |
| `github_account.py` | Starts GitHub CLI browser sign-in and reports the connected account name without reading tokens. |
| `self_update.py` | Checks public releases, verifies the published ZIP digest and manifest, backs up old source, and updates while tasks are idle. |
| `ollama_runtime.py` | Migrates older portable Ollama installs to the native Windows application after project and model workers are idle. |
| `chatgpt_auth.py` | Public-client ChatGPT sign-in, identity validation, DPAPI token storage, refresh, and Responses API adaptation. |
| `configure_install.py` | Creates the main and vision Ollama models and configures Hermes. |
| `toolbox_mcp.py`, `web_tools.py`, `media_worker.py`, `small_training.py` | Tools available to the agent for files, browsers, media, and small-model training. The public page reader can select query-relevant passages across long pages while preserving exact-find and pagination modes. |
| `chat_assistant.py`, `file_chat.py` | Route short chat requests and answer bounded questions about selected local files using installed models. Long files use lexical passage selection across the document before inference. |
| `quick_calculator.py` | Calculates explicit chat arithmetic with exact rational values and a bounded numeric grammar, before a model is consulted. |
| `system_gauges.py` | Samples Windows CPU and RAM load, NVIDIA GPU load, drive capacity and PC-wide disk/network transfer counters for live header rings. Missing sensors stay visibly unavailable. |
| `pc_offline.py`, `pc_offline.ps1` | Request elevation for a PC-wide physical-adapter switch, record exactly which adapters were disabled, restore those adapters on request, and suppress cloud routing while the offline record exists. |
| `install-files.json`, `pack-release.py`, `build-release.ps1` | Explicit list of shipped files and Windows release packaging. |

The Hermes agent itself is an external pinned dependency, installed under the app's `data` directory. Cale AI Workbench adds the task supervisor and tools around it. See `downloads.json` for pinned downloads and `THIRD-PARTY-NOTICES.md` for upstream licenses.

## Tasks and state

The UI inserts user requests into `LongTasks/projects.db`. The monitor handles them through a separate `requests` table, so a button click does not block the window. Completed follow-ups are mirrored to `.local-ai/USER-REQUESTS.md` inside the project, while the original brief stays bounded. A new worker gets only the latest three follow-ups in its first message and must read the full file before planning; the file is rebuilt from SQLite at monitor startup, after each new follow-up, and before each new worker card. Older briefs are compacted only when their appended notes exactly match the queue. The monitor creates an idempotent Hermes board card for each task generation. The worker writes `.local-ai/PLAN.md`, `PROGRESS.md`, and `verify.ps1` inside the project folder. A fresh worker receives a bounded snapshot of the saved plan and latest progress in its board brief, then reads the current files. The supervisor runs `verify.ps1` in a Windows Job object, then starts a distinct review pass. Successful review reaches `ready_for_review`; the user accepts it. Failed review verification creates a new generation card with the failure evidence; user feedback after review does the same. Stop and Delete retire the board work but preserve project files. Every cycle processes task requests even when model or gateway startup fails, and one task's retryable error does not prevent later tasks from advancing.

`project_memory.py` creates `.local-ai/memory.sqlite3` when a project is created and initializes it for existing projects at supervisor startup. Its FTS5 index refreshes plan/progress documents during search; the agent can explicitly scan source files when needed. File-backed entries are indexed by content hash, so edited or deleted documents do not leave stale search results. Separate records capture durable decisions, lessons, app components, and skill recipes. The database and its WAL files stay under `.local-ai`, outside completion fingerprints; project source files remain authoritative. MCP memory tools accept only folders registered in the local project queue.

The UI polls every five seconds. Existing task widgets are updated in place; a widget is created or destroyed only when a task is added or removed. Mouse-wheel scrolling is scoped to the task list, and the canvas keeps its pixel position across ordinary refreshes. Progress percentages are estimates from the saved milestone checklist, not a time estimate. The supervisor hashes project contents before and after independent verification and again before acceptance. A verification script that changes source files must be followed by a fresh check. Legacy metadata-only checksums are accepted only for migration after another real verification pass.
Model sizing extracts the original request from the persistent board brief. A simple question can use the fast tier, current facts that need public verification use a middle tier, and build work uses a deeper tier. Tool history can raise the tier as a task grows.
Hermes `scheduled` cards remain active while waiting for their next dispatch. Only blocked, triage, or archived cards require attention; treating scheduled work as blocked would stop a long-running project between worker runs.

`LongTasks/install-progress.json` is read by the UI during first-run setup. The optional downloader writes `LongTasks/optional-model-*.json` in the background, uses a Windows file lock to prevent duplicate automatic runs, and leaves weights in Ollama's model store. It checks Ollama for a usable local model before trusting a saved completion status, and checks physical RAM plus at least 12 GB of extra space on the model store's drive before each pull. A catalog signature makes the app rescan when a release adds or changes model sources. Optional model downloads are kept off the main task screen.

The public `models/recommendations.json` feed can add official Ollama models without a full app release. New entries are bounded to Ollama library sources; after download, a real local function-call probe must pass before routing uses the model. On launch, `self_update.py` checks GitHub releases, verifies the ZIP's published SHA-256 digest, and installs source changes once tasks are idle. It backs up replaced source in `Updates/backups` and excludes `Projects`, `LongTasks`, `Cloud`, model weights, and runtime folders.

## Accounts and model routing

Cloud credentials are encrypted for the current Windows user with DPAPI under `Cloud/`; they are never included in the release. The local loopback router requires its own random bearer token. ChatGPT connection and ChatGPT plan use are separate choices. The router only tries that plan after the user enables it and for demanding tasks. Otherwise it ranks installed local models by task size and tool capability; free OpenRouter is a fallback if local inference fails. The list of usable models refreshes as background downloads complete.

The router records provider choice and error categories in `Cloud/routing.jsonl`, not prompts, tool results, or account keys. `cloud_ai.py` exposes an OpenAI-compatible loopback endpoint because Hermes expects chat-completion-shaped messages; `chatgpt_auth.py` translates them to OpenAI Responses API requests when the user opts in.
On a model refusal, the router tries installed derestricted local models. It prefers the small tool-probed model for short requests and the large coding model for deep projects. A model advertised as tool-capable is not promoted from the automatic catalog until it makes a correct local test function call.

## Changing and testing code

See [CONTRIBUTING.md](CONTRIBUTING.md). Keep behavior and safety checks in the relevant module, update the README when user-visible behavior changes, and add each new shipped file to `install-files.json`. The source ZIP and EXE embed the same manifest-defined payload.
