# Cale AI Workbench

**A Windows assistant that builds with tools, saves its progress, and runs local models.**

Created by [Cale Media](https://calemedia.com/). Built on Hermes Agent, Ollama, and open-source developer tools.

## Install

**[Download the Windows installer](https://github.com/CaleMediaDev/cale-ai-workbench/releases/latest)**

1. Download `Cale-AI-Workbench-Setup.exe` and open it.
2. Setup begins automatically. The workbench opens during setup and shows downloads under **My tasks**.
3. When setup reaches 100%, click **New task** and describe what you want done in everyday language. The app creates a task folder for you.

The installer downloads the runtime, tools, browser, image/voice models, and model files. No Python, Git, Node, terminal commands, or API keys need to be installed manually. The ZIP alternative contains `Install.cmd` with the same setup screen.

**Windows x64, 16 GB RAM minimum.** Automatic setup chooses a smaller model for smaller PCs. The large coding model needs about 96 GB RAM and downloads roughly 52 GB. The installer also downloads a 9B vision model, a diffusion image model, speech recognition weights, and a small training base. Allow at least 32 GB more disk space than the chosen coding model's download size. The initial download needs internet; installed local models can run offline. Public web tools still require internet.

This is an **early preview**, with an unsigned Windows installer. It has been developed and tested on Windows with an RTX 3060. Other hardware configurations and uninterrupted multi-week operation have not been verified.

## What it can do

- Work through a saved project plan, build in milestones, run tests, and recover from saved progress.
- Track task milestones with progress bars and the current step. Pause, stop, delete, resume, receive new instructions, and review completed work. Deleting a task removes it from the list but preserves its project files.
- Use files, terminal commands, public web search, browser interactions, document extraction, and database queries.
- Discover specialist REA and Chrome DevTools operations without loading hundreds of tool schemas into every prompt.
- Inspect JavaScript, analyze Windows binaries, and use Ghidra where supported.
- Connect your own free OpenRouter account with one browser sign-in, then fall back to local inference when limits or errors occur. No API key copying is needed.
- Optionally connect ChatGPT and explicitly enable your shared ChatGPT/Codex plan allowance for tasks. Connecting alone does not use that allowance.
- Start optional large-model downloads from task cards, including the linked Huihui and ArliAI derivatives.
- Generate images locally, describe photos and screenshots, transcribe audio, and create offline spoken WAV files.
- Inspect sampled video frames and optionally transcribe a video's audio. Agents can capture a camera frame or watch up to four snapshots over 30 seconds only after you enable Camera in the app; that switch expires after one hour.
- Train a small text classifier from labeled CSV rows or a separate 0.5B LoRA adapter from JSONL examples, with held-out evaluations saved beside the result.

The agent's terminal can modify files with your Windows permissions. Choose a specific project folder and keep normal backups. It asks for review; passing tests does not prove an app is correct or production-ready.

The image model uses LCM Dreamshaper v7; speech recognition uses Faster-Whisper base; the training base is Qwen2.5-0.5B-Instruct. The local image generator and voice tools are separate from the main coding model. Camera capture is off by default. When a task requests camera use, its card offers a one-hour permission button. Video analysis samples up to eight frames, so it can miss activity between them. Training does not let the assistant continuously learn from the camera or alter the main coding model.

## Long tasks

The project queue and native Hermes task board use SQLite. The worker maintains `.local-ai/PLAN.md` and `PROGRESS.md`. The supervisor runs the project's verification script independently, starts a separate review pass, and checks again before acceptance. Repeated verification failures stop for input rather than retrying forever.

Your PC must be on and awake. The optional sign-in task restarts the supervisor when you sign back in. This is resumable work across days, not a promise that any request will complete unattended. One demanding local worker runs at a time.

## Free cloud connection

When setup finishes, the app opens an OpenRouter browser connection automatically. Sign in and approve the connection once; the app saves the resulting key privately for that Windows account. You never need to copy an API key. The rocket button at the top opens **Supercharge my AI**, where you can reconnect OpenRouter or connect ChatGPT. Connecting allows task prompts and tool results to be sent to the selected online provider; an in-flight request may finish after disconnecting.

ChatGPT connection uses OpenAI's public-client Sign in with ChatGPT flow. It verifies the returned identity and stores OAuth tokens with Windows DPAPI. ChatGPT plan use stays off until you explicitly enable it. When enabled, eligible task requests draw from the same ChatGPT/Codex allowance as other connected apps, not from a separate free pool. If that route fails, the app tries OpenRouter free models and then the local model. ChatGPT plan use is subject to account eligibility and the [current OpenAI preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations). Review or limit usage in [ChatGPT settings](https://chatgpt.com/settings/usage).

The router uses `openrouter/free`, forces zero maximum provider prices, strips alternative model routes and plugins, and falls back to Ollama. OpenRouter's free quotas and availability are limited and can change. Keys you add yourself are encrypted with Windows DPAPI; they are not written into the Hermes configuration or routing logs.

No account keys are included in the repository or installer. Local models work without a cloud service.

See [OpenRouter's free model information](https://openrouter.ai/collections/free-models/) and [account connection documentation](https://openrouter.ai/docs/guides/overview/auth/oauth).

## Models included in the project

The repository contains model definitions, sources, configuration, and automatic download code. The installer places the actual weights in Ollama's local model store. It does not put tens of gigabytes into Git history or require users to find model files themselves.

| Model | Approximate download | Suggested RAM | Role |
|---|---:|---:|---|
| Qwen3-Coder-Next Q4 | 52 GB | 96 GB | Main coding model on large PCs |
| Qwen 3.5 9B | 7 GB | 24 GB | Smaller default |
| Qwen 3.5 4B | 4 GB | 16 GB | Light default |
| Huihui Qwen Coder Next abliterated | 52 GB | 96 GB | Optional local coding variant |
| ArliAI GPT-OSS 20B Derestricted | 13 GB | 32 GB | Experimental optional model |
| Huihui Llama 3.3 70B abliterated | 43 GB | 80 GB | Experimental optional model |
| ArliAI GLM 4.5 Air Derestricted | 67 GB | 112 GB | Experimental optional model |

RAM guidance is conservative, not a benchmark. Speed depends heavily on GPU memory, CPU and context size. Setup downloads the recommended base model, vision model, image, speech, and training weights automatically. Optional large models appear as cards under **My tasks** with Start download buttons; the app checks for advertised tool support before using them. “Abliterated” or “derestricted” describes the publisher's modifications; it does not establish better reasoning, reliability, or suitability for a particular task. See [model sources and licenses](THIRD-PARTY-NOTICES.md).

## Development

The companion source is MIT licensed. Third-party runtimes, model weights and dependencies keep their own licenses.

The primary files are `long_tasks.py`, `cloud_ai.py`, `chatgpt_auth.py`, `toolbox_mcp.py`, `model_library.py`, `web_tools.py`, `media_worker.py`, and `small_training.py`. `config-template.yaml` contains no account secrets. `Install-Backend.ps1` performs the same setup used by the graphical installer.

After installation, run `Toolbox/python/Scripts/python.exe Validation/test_cloud_ai.py` and `Toolbox/python/Scripts/python.exe Validation/test_long_tasks.py` from the installation directory. Tests cover real process termination, recovery state, stale verification, OAuth callback validation, free-only routing and fallback. They do not require a cloud account.

Please report reproducible bugs and include versions and redacted errors. Do not upload your `Cloud`, `data`, `Projects`, `LongTasks`, or log directories.

Made by [Cale Media — websites and digital marketing](https://calemedia.com/).
