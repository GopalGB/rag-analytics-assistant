# Models, software, licences and recurring costs

Licence details are summarised for convenience. Check each project's current licence before a
production deployment.

## AI models (local, via Ollama)

| Model | Size on disk | Licence | Commercial use | Role |
|---|---|---|---|---|
| Qwen 2.5 14B Instruct (`qwen2.5:14b`, default) | ~9 GB | Apache 2.0 | Yes | Answers, tool use, invoice assist |
| Qwen 2.5 32B Instruct (`qwen2.5:32b`) | ~20 GB | Apache 2.0 | Yes | Higher quality on 64 GB+ Macs |
| Llama 3.3 70B Instruct (`llama3.3:70b`) | ~43 GB | Llama 3.3 Community Licence | Yes, with conditions (attribution, acceptable-use policy) | Highest quality on 128 GB Macs |
| nomic-embed-text v1.5 (`nomic-embed-text`) | ~270 MB | Apache 2.0 | Yes | Semantic search embeddings |
| Qwen 2.5 3B Instruct (tested here on CPU only) | ~2 GB | Qwen Research Licence | **No**: non-commercial | Not recommended |

No model is trained or fine-tuned; existing models are used as-is. Search uses `nomic-embed-text`
when Ollama has it, and otherwise a built-in offline hashing embedder (no download). Embeddings are
cached on disk, so unchanged documents are never embedded twice.

## Software

| Component | Purpose | Licence |
|---|---|---|
| Python 3.10+ | Runtime | PSF |
| FastAPI, Starlette, Uvicorn | Web server / API | MIT, BSD-3, BSD-3 |
| python-multipart | File uploads | Apache 2.0 |
| DuckDB | Local analytical database | MIT |
| pandas, NumPy | Tables, vectors | BSD-3 |
| Pydantic, pydantic-settings | Validation, configuration | MIT |
| pypdf | PDF text and image extraction | BSD-3 |
| Pillow | Image handling | MIT-CMU (HPND) |
| openpyxl | Excel files | MIT |
| requests | HTTP (Ollama, Intuit) | Apache 2.0 |
| Tesseract OCR | Local OCR for scans and photos | Apache 2.0 |
| Ollama | Local model runtime (Apple Silicon GPU) | MIT |
| keyring (optional) | macOS Keychain token storage | MIT |
| boto3 (optional) | AWS Bedrock, only if cloud AI is approved | Apache 2.0 |
| pytest, httpx, ruff (dev) | Tests, lint | MIT, BSD-3, MIT |
| reportlab, python-docx (dev) | Generating the synthetic sample documents | BSD, MIT |

Exact versions: `requirements.lock.txt`. The application code is MIT-licensed (`LICENSE`).

## Recurring costs

| Item | Cost | Notes |
|---|---|---|
| Local models, OCR, database, all software above | $0 | Open source; runs on the existing Mac Studio |
| Electricity / hardware | existing | Inference runs on the Mac Studio GPU |
| Intuit developer account + sandbox company | $0 | Needed for the live sandbox mode |
| QuickBooks Online subscription | existing | The company's own subscription (production stage only) |
| Intuit API usage (production) | check current terms | Intuit's App Partner Program meters some API usage for production apps; a read-only daily sync is low volume |
| Cloud AI (optional, off by default) | pay-per-use per provider | Only if approved. Set `LLM_PRICING` to see estimated spend per model in the **AI models** tab; routing simple tasks to the fast tier keeps costs down |
| Support / maintenance | to be agreed | See ROADMAP.md |
| Hosted public demo (optional) | pay-per-use | Groq's list rates for `openai/gpt-oss-120b` on 2026-09-24: USD 0.15 per 1M input tokens (0.075 cached) and USD 0.60 per 1M output tokens. List rates, not measured spend. Vercel and Cloudflare have free tiers; check current limits. See [DEPLOYMENT.md](DEPLOYMENT.md) |

## Cloud AI providers (optional)

Anthropic, OpenAI, Google Gemini, OpenRouter, Azure OpenAI, Groq, Mistral, DeepSeek, Together AI, xAI
and AWS Bedrock are supported through their HTTP APIs (no extra SDKs, except `boto3` for Bedrock).
Each is used only when its API key is set and `ALLOW_CLOUD_AI=true`. Review each provider's
data-retention and training terms before approval; prefer business/API tiers that exclude training.

## Open-weight models used through a hosted API (public demo only)

| Model | Licence | Notes |
|---|---|---|
| gpt-oss-120b / gpt-oss-20b (OpenAI open-weight) | Apache 2.0 | Served by Groq in the hosted demo (https://console.groq.com/docs/model/openai/gpt-oss-120b). The model licence is separate from the hosting provider's service terms. |

## Bundled public documents

Four real public documents are part of the sample data (public domain, OGL v3.0, CC BY-SA 4.0, PDDL);
sources and licences are listed in [PUBLIC-DOCUMENTS.md](PUBLIC-DOCUMENTS.md).

## Internet access needed

- **Install and updates**: Homebrew, Python packages (PyPI), Ollama model downloads, `git pull`.
- **Cloud AI, only if approved**: the API host of each provider you configured.
- **Live QuickBooks sandbox mode only**: `appcenter.intuit.com`, `oauth.platform.intuit.com`,
  `developer.api.intuit.com` (revoke), `sandbox-quickbooks.api.intuit.com`.
- Nothing else. Day-to-day use with default settings works fully offline.
