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
| Ghidra | https://github.com/NationalSecurityAgency/ghidra |
| Eclipse Temurin JDK | https://adoptium.net/temurin/releases/ |
| Node.js | https://nodejs.org/ |
| ast-grep | https://github.com/ast-grep/ast-grep |
| avoid-ai-design scanner (MIT; license bundled under `vendor/avoid-ai-design/LICENSE`) | https://github.com/funboy322/avoid-ai-design |
| Addy Osmani's Agent Skills (MIT; seven selected skills pinned at commit `8bfcc1f9916f2eea55494d8c1fa7f4bde867dd9e`; license bundled under `vendor/agent-skills/LICENSE`) | https://github.com/addyosmani/agent-skills |
| Diffusers and LCM Dreamshaper v7 | https://huggingface.co/SimianLuo/LCM_Dreamshaper_v7 |
| Segmind SSD-1B (Apache-2.0 image model; downloaded on supported PCs) | https://huggingface.co/segmind/SSD-1B |
| Faster-Whisper and PyAV | https://github.com/SYSTRAN/faster-whisper |
| Qwen2.5-0.5B-Instruct training base | https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct |
| PyTorch | https://pytorch.org/ |
| uv | https://github.com/astral-sh/uv |
| ACE-Step 1.5 (MIT; optional local music engine installed from pinned upstream source on first use) | https://github.com/ace-step/ACE-Step-1.5 |

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
