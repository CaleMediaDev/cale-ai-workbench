# Third-party components

Cale AI Workbench integrates these projects. Their original authors retain ownership. Our MIT license does not replace dependency or model licenses. The installer downloads dependencies from their upstream distributions; it does not redistribute account data or pretrained weights inside the source repository.

| Component | Upstream |
|---|---|
| Hermes Agent, MIT, Copyright (c) 2025 Nous Research | https://github.com/NousResearch/hermes-agent |
| Ollama | https://github.com/ollama/ollama |
| GitHub CLI | https://github.com/cli/cli |
| Kokoro-ONNX (MIT) and Kokoro 82M voice weights (Apache-2.0) | https://github.com/thewh1teagle/kokoro-onnx and https://huggingface.co/hexgrad/Kokoro-82M |
| Qwen3-TTS 0.6B Base voice clone (Apache-2.0; downloaded locally) | https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base |
| REA / rea-agents | https://github.com/morluto/rea |
| Chrome DevTools MCP | https://github.com/ChromeDevTools/chrome-devtools-mcp |
| Playwright MCP | https://github.com/microsoft/playwright-mcp |
| Crawl4AI (Apache-2.0), local browser crawler | https://github.com/unclecode/crawl4ai |
| Hermes Connector 0.2.6 (MIT; license bundled under `vendor/hermes-connector/LICENSE`) | https://github.com/CorsenAI/hermes-connector/tree/v0.2.6 |
| MarkItDown | https://github.com/microsoft/markitdown |
| pypdfium2 5.14.0 (BSD-3-Clause/Apache-2.0; local scanned-PDF rendering) | https://github.com/pypdfium2-team/pypdfium2 |
| Ghidra | https://github.com/NationalSecurityAgency/ghidra |
| Eclipse Temurin JDK | https://adoptium.net/temurin/releases/ |
| Node.js | https://nodejs.org/ |
| ast-grep | https://github.com/ast-grep/ast-grep |
| avoid-ai-design scanner (MIT; license bundled under `vendor/avoid-ai-design/LICENSE`) | https://github.com/funboy322/avoid-ai-design |
| michaelvereb/ai-ui 1.2.0 Astro component kit (MIT; selected components pinned at commit `73afdce6c3bc6bd33e1f8656184fbd5980e2e50d`; license bundled under `vendor/ai-ui/LICENSE`; local-default text and offline styling modified) | https://github.com/michaelvereb/ai-ui |
| Addy Osmani's Agent Skills (MIT; seven selected skills pinned at commit `8bfcc1f9916f2eea55494d8c1fa7f4bde867dd9e`; license bundled under `vendor/agent-skills/LICENSE`) | https://github.com/addyosmani/agent-skills |
| Diffusers and LCM Dreamshaper v7 | https://huggingface.co/SimianLuo/LCM_Dreamshaper_v7 |
| Segmind SSD-1B (Apache-2.0 image model; downloaded on supported PCs) | https://huggingface.co/segmind/SSD-1B |
| Faster-Whisper and PyAV | https://github.com/SYSTRAN/faster-whisper |
| Qwen2.5-0.5B-Instruct training base | https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct |
| PyTorch | https://pytorch.org/ |
| uv | https://github.com/astral-sh/uv |
| ACE-Step 1.5 (MIT; optional local music engine installed from pinned upstream source on first use) | https://github.com/ace-step/ACE-Step-1.5 |
| Audio Separator 0.48.0 (MIT; optional local vocal/instrumental separation installed on first use; model weights download separately) | https://github.com/nomadkaraoke/python-audio-separator |
| Spotify Basic Pitch 0.4.0 (Apache-2.0; optional local audio-to-MIDI transcription installed on first use) | https://github.com/spotify/basic-pitch |
| Matchering 2.0.6 (GPL-3.0; optional separate local reference-mastering runtime installed on first use, not bundled in the release) | https://github.com/sergree/matchering |
| imageio-ffmpeg 0.6.0 (BSD-2-Clause; bundles FFmpeg binary used for local trim, fade and mix; FFmpeg carries its own component licenses) | https://github.com/imageio/imageio-ffmpeg |
| OpenVideo Studio (MIT; pinned optional local Wan/ComfyUI installer fetched on first video generation) | https://github.com/melayyoub/openvideo-studio |
| ComfyUI (GPL-3.0; installed separately on first video generation) | https://github.com/Comfy-Org/ComfyUI |
| Wan 2.2 (Apache-2.0; checksum-verified model files downloaded separately) | https://huggingface.co/Comfy-Org/Wan-2.2 |
| public-apis-live (MIT; its daily catalog is queried on demand for public API research leads, not bundled) | https://github.com/Manavarya09/public-apis-live |
| scikit-learn 1.9.1 (BSD-3-Clause; local CSV classification and regression training, installed as a Python dependency) | https://github.com/scikit-learn/scikit-learn |
| OpenCV Zoo YuNet face detector (MIT) and SFace recognizer (Apache-2.0); checksum-verified weights download to the user's PC when camera greeting is enabled | https://github.com/opencv/opencv_zoo |

The Windows compatibility patch contains modifications to MIT-licensed Hermes code. The upstream MIT notice is reproduced in `HERMES-LICENSE`.

## Model sources

- Qwen3-Coder-Next: https://huggingface.co/Qwen/Qwen3-Coder-Next — Apache-2.0. Ollama distribution: https://ollama.com/library/qwen3-coder-next.
- Qwen 3.5: https://ollama.com/library/qwen3.5 — consult the selected model's upstream license.
- Huihui Qwen Coder Next abliterated: https://huggingface.co/huihui-ai/Huihui-Qwen3-Coder-Next-abliterated — Apache-2.0. Publisher's Ollama distribution: https://ollama.com/huihui_ai/qwen3-coder-next-abliterated.
- Huihui Qwen 3.5 9B abliterated: https://huggingface.co/huihui-ai/Huihui-Qwen3.5-9B-abliterated — Apache-2.0. Publisher's Ollama distribution: https://ollama.com/huihui_ai/qwen3.5-abliterated:9b.
- Huihui Llama 3.3 70B abliterated: https://huggingface.co/huihui-ai/Llama-3.3-70B-Instruct-abliterated — **Llama 3.3 Community License**, not our MIT license. Built with Llama. Publisher's distribution: https://ollama.com/huihui_ai/llama3.3-abliterated.
- ArliAI GPT-OSS 20B Derestricted: https://huggingface.co/ArliAI/gpt-oss-20b-Derestricted — Apache-2.0. Community GGUF conversion: https://huggingface.co/MaziyarPanahi/gpt-oss-20b-Derestricted-GGUF.
- OpenAI GPT-OSS 20B via Ollama: https://ollama.com/library/gpt-oss — Apache-2.0; downloaded separately from this source release.
- Qwen 3.5 35B via Ollama: https://ollama.com/library/qwen3.5 — Apache-2.0; downloaded separately from this source release.
- Qwen 3.6 35B via Ollama: https://ollama.com/library/qwen3.6 — Apache-2.0; offered by the public recommendation feed when the PC has enough RAM and storage.
- ArliAI GLM 4.5 Air Derestricted: https://huggingface.co/ArliAI/GLM-4.5-Air-Derestricted — MIT. Community GGUF conversion: https://huggingface.co/mradermacher/GLM-4.5-Air-Derestricted-GGUF.

Model names and logos belong to their respective owners. This project is not affiliated with those publishers, OpenRouter, or OpenAI. No claim is made that community derivatives retain the benchmarks of their base models.

LCM Dreamshaper v7 derives from Dreamshaper and Stable Diffusion 1.5. Review the upstream model card and base licenses before redistribution or commercial use of generated material. The installer downloads these weights to the user's PC; this repository and release installer do not bundle them.


## Optional subscription agents

Claude Code and Grok Build are separate vendor programs, downloaded only when the user selects their connection. Their executables and credentials are not included in Workbench releases. Workbench verifies the pinned official package checksum and runs the native binary without modifications. Each person signs in through the vendor's own flow and remains subject to their plan, usage limits and vendor terms. Names are used to identify the optional connections; no affiliation or endorsement is implied.

- Claude Code: https://code.claude.com/docs/en/legal-and-compliance and https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan.
- Grok Build: https://github.com/xai-org/grok-build (Apache-2.0 source) and https://docs.x.ai/build/overview.


## Integrated DJ audio

The Music page uses the public LibVLC 3 API. Cale downloads the unmodified
VideoLAN VLC 3.0.24 Windows x64 portable archive on first use and verifies the
published SHA-256 before extraction. The portable runtime is separate from the
Cale installer and includes its upstream notices and licenses. LibVLC's public
API headers are LGPL-2.1-or-later. See the [upstream source and downloads](https://downloads.videolan.org/pub/videolan/vlc/3.0.24/)
and [LibVLC API](https://raw.githubusercontent.com/videolan/vlc/3.0.x/include/vlc/libvlc.h).

Transport and load icons are from [Phosphor Icons](https://github.com/phosphor-icons/core/tree/2b75f3ad12b420c9504ef05df8d2564a28f8500e), rasterized for the native controller.
MIT License

Copyright (c) 2023 Phosphor Icons

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
