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
| `project_preview.py`, `preview_capture.js` | Select local task previews and capture web pages into a side window; Chrome DevTools actions also mirror their selected page through `toolbox_mcp.py`. |
| `model_library.py` | Defines optional Ollama models, checks RAM and disk space, downloads sequentially, and writes private progress JSON. |
| `auto_route.py`, `cloud_ai.py` | Sizes each request without an extra AI call, chooses an installed tool-capable local model, and handles explicit ChatGPT plan use plus free OpenRouter fallback. |
| `model_identity.py` | Reads the installed Hermes model name so source updates preserve older working Ollama aliases. |
| `voice_assistant.py`, `natural_voice.py` | Run offline speech recording and playback; upgrade existing installations to pinned Kokoro voice weights in the background. |
| `github_account.py` | Starts GitHub CLI browser sign-in and reports the connected account name without reading tokens. |
| `self_update.py` | Checks public releases, verifies the published ZIP digest and manifest, backs up old source, and updates while tasks are idle. |
| `ollama_runtime.py` | Migrates older portable Ollama installs to the native Windows application after project and model workers are idle. |
| `chatgpt_auth.py` | Public-client ChatGPT sign-in, identity validation, DPAPI token storage, refresh, and Responses API adaptation. |
| `configure_install.py` | Creates the main and vision Ollama models and configures Hermes. |
| `toolbox_mcp.py`, `web_tools.py`, `media_worker.py`, `small_training.py` | Tools available to the agent for files, browsers, media, and small-model training. |
| `install-files.json`, `pack-release.py`, `build-release.ps1` | Explicit list of shipped files and Windows release packaging. |

The Hermes agent itself is an external pinned dependency, installed under the app's `data` directory. Cale AI Workbench adds the task supervisor and tools around it. See `downloads.json` for pinned downloads and `THIRD-PARTY-NOTICES.md` for upstream licenses.

## Tasks and state

The UI inserts user requests into `LongTasks/projects.db`. The monitor handles them through a separate `requests` table, so a button click does not block the window. It creates an idempotent Hermes board card for each task generation. The worker writes `.local-ai/PLAN.md`, `PROGRESS.md`, and `verify.ps1` inside the project folder. A fresh worker receives a bounded snapshot of the saved plan and latest progress in its board brief, then reads the current files. The supervisor runs `verify.ps1` in a Windows Job object, then starts a distinct review pass. Successful review reaches `ready_for_review`; the user accepts it. Stop and Delete retire the board work but preserve project files.

The UI polls every five seconds. Existing task widgets are updated in place; a widget is created or destroyed only when a task is added or removed. Mouse-wheel scrolling is scoped to the task list, and the canvas keeps its pixel position across ordinary refreshes. Progress percentages are estimates from the saved milestone checklist, not a time estimate.
Hermes `scheduled` cards remain active while waiting for their next dispatch. Only blocked, triage, or archived cards require attention; treating scheduled work as blocked would stop a long-running project between worker runs.

`LongTasks/install-progress.json` is read by the UI during first-run setup. The optional downloader writes `LongTasks/optional-model-*.json` in the background, uses a Windows file lock to prevent duplicate automatic runs, and leaves weights in Ollama's model store. It checks Ollama for a usable local model before trusting a saved completion status, and checks physical RAM plus at least 12 GB of extra space on the model store's drive before each pull. A catalog signature makes the app rescan when a release adds or changes model sources. Optional model downloads are kept off the main task screen.

The public `models/recommendations.json` feed can add official Ollama models without a full app release. New entries are bounded to Ollama library sources; after download, a real local function-call probe must pass before routing uses the model. On launch, `self_update.py` checks GitHub releases, verifies the ZIP's published SHA-256 digest, and installs source changes once tasks are idle. It backs up replaced source in `Updates/backups` and excludes `Projects`, `LongTasks`, `Cloud`, model weights, and runtime folders.

## Accounts and model routing

Cloud credentials are encrypted for the current Windows user with DPAPI under `Cloud/`; they are never included in the release. The local loopback router requires its own random bearer token. ChatGPT connection and ChatGPT plan use are separate choices. The router only tries that plan after the user enables it and for demanding tasks. Otherwise it ranks installed local models by task size and tool capability; free OpenRouter is a fallback if local inference fails. The list of usable models refreshes as background downloads complete.

The router records provider choice and error categories in `Cloud/routing.jsonl`, not prompts, tool results, or account keys. `cloud_ai.py` exposes an OpenAI-compatible loopback endpoint because Hermes expects chat-completion-shaped messages; `chatgpt_auth.py` translates them to OpenAI Responses API requests when the user opts in.

## Changing and testing code

See [CONTRIBUTING.md](CONTRIBUTING.md). Keep behavior and safety checks in the relevant module, update the README when user-visible behavior changes, and add each new shipped file to `install-files.json`. The source ZIP and EXE embed the same manifest-defined payload.
