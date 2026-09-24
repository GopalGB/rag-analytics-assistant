# Installing on a Mac Studio

Everything below runs locally. Only the downloads in steps 1–3 use the internet.

## 1. Prerequisites

```bash
# Homebrew (skip if installed): https://brew.sh
brew install python@3.12 tesseract ollama git
```

- **Python 3.10+** runs the assistant.
- **Tesseract** is the local OCR engine for scanned PDFs and photos. For other languages:
  `brew install tesseract-lang` and set `OCR_LANG=eng+fra` (for example).
- **Ollama** runs AI models on the Apple Silicon GPU.

Turn on **FileVault** (System Settings → Privacy & Security → FileVault) so documents, the database
and the activity log are encrypted at rest.

## 2. Get the code and install

```bash
git clone <repository-url> private-ai-assistant
cd private-ai-assistant
make setup
```

## 3. Choose and download a local model

Start Ollama (open the Ollama app, or run `ollama serve`), then pull one model:

| Mac Studio memory | Suggested model | Download | Notes |
|---|---|---|---|
| 32 GB | `qwen2.5:14b` (default) | ~9 GB | Good tool use and document answers; fast |
| 64 GB | `qwen2.5:32b` | ~20 GB | Better reasoning over long documents |
| 128 GB+ | `llama3.3:70b` or `qwen2.5:72b` | ~43–47 GB | Highest quality; slower; check the licence (see DEPENDENCIES.md) |

```bash
ollama pull qwen2.5:14b
ollama pull nomic-embed-text     # small embedding model for semantic search (~270 MB)
```

Use a model with **native tool calling** (Qwen 2.5, Llama 3.1+). Very small models (≤3B) can answer
from documents but don't reliably query tables (see [TEST-RESULTS.md](TEST-RESULTS.md)).

To use a different model: `cp env.example .env` and set `OLLAMA_MODEL=qwen2.5:32b`.

## 4. Run

```bash
make run
```

Open <http://127.0.0.1:8000>. The header shows the AI model (should say *local*), OCR status and
the QuickBooks connection. The **Privacy & security** tab confirms nothing leaves the machine.

The server listens on `127.0.0.1` only. To allow other computers on the office network, set
`APP_API_KEY` in `.env` and start it with `--host 0.0.0.0`. Put it behind HTTPS (e.g. Caddy) and
set `TRUST_LOOPBACK=false`.

## 5. Start automatically at login (optional)

Save as `~/Library/LaunchAgents/local.private-ai-assistant.plist` (adjust the path):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.private-ai-assistant</string>
  <key>WorkingDirectory</key><string>/Users/YOU/private-ai-assistant</string>
  <key>ProgramArguments</key><array>
    <string>/Users/YOU/private-ai-assistant/.venv/bin/uvicorn</string>
    <string>app.main:app</string><string>--host</string><string>127.0.0.1</string><string>--port</string><string>8000</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/private-ai-assistant/storage/server.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/private-ai-assistant/storage/server.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/local.private-ai-assistant.plist
```

## 6. Point it at your own documents (after approval)

The prototype ships with synthetic data. When approved to use real documents, set
`DATA_DIR=/path/to/folder` in `.env`. Subfolders work: files under a folder named `invoices` are
treated as invoices, everything else is classified by content. Start with a small, non-confidential
set and check the results in the Invoices tab before widening it.

## Verify the install

```bash
make test        # full automated test suite
make evaluate    # regenerates docs/TEST-RESULTS.md on this machine
```
