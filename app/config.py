"""Central configuration. All values come from environment variables (or a local .env file).

Field names map case-insensitively to environment variables, e.g. `app_api_key` <- `APP_API_KEY`.
Defaults are chosen for a private, local-first prototype: local model, local embeddings, local OCR,
offline QuickBooks sandbox fixture, no cloud AI unless explicitly approved.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "Private AI Assistant"
    data_dir: str = "data/sample"  # documents, invoices and spreadsheets to index (scanned recursively)
    storage_dir: str = "storage"  # local state: database, caches, reviews, approvals, audit log
    db_path: str = "storage/assistant.duckdb"
    upload_subdir: str = "uploads"  # uploads are saved under <data_dir>/<upload_subdir>/
    max_upload_bytes: int = 20 * 1024 * 1024

    # --- HTTP security ---
    app_api_key: str | None = None  # optional shared key; if set, non-public routes require it
    rate_per_minute: int = 60
    rate_burst: int = 20
    max_body_bytes: int = 64 * 1024
    max_input_chars: int = 2000
    # Loopback exemption for the API key. Safe for local use; set False behind a reverse proxy
    # (where every request appears to come from 127.0.0.1) so the key is always required.
    trust_loopback: bool = True

    # --- AI model ---
    # With no model the assistant still works: document questions return the most relevant source
    # passages (extractive mode) and invoices are extracted by local rules. Set True to refuse to
    # start without a model.
    require_llm: bool = False
    llm_provider: str = "auto"  # auto | ollama | openai | bedrock | cli | none
    llm_timeout: int = 300
    # Privacy gate: cloud AI (OpenAI, Bedrock, or a CLI that calls a cloud API) is refused unless the
    # owner has approved it. Local models (Ollama, or an OpenAI-compatible server on localhost) are fine.
    allow_cloud_ai: bool = False
    # Ollama (local, recommended on a Mac Studio): https://ollama.com
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model: str | None = "qwen2.5:14b"
    # OpenAI (or any OpenAI-compatible /chat/completions endpoint, incl. local servers like LM Studio)
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    # AWS Bedrock (needs boto3 + AWS credentials in the environment)
    bedrock_model_id: str | None = None
    aws_region: str = "us-east-1"
    # CLI backend: a shell command that reads the prompt on stdin and writes the completion to stdout.
    llm_cli_command: str | None = None
    llm_cli_timeout: int = 120
    llm_cli_is_local: bool = False  # set True only if the CLI runs a model on this machine

    # --- Embeddings (vectorization; local by default — no model download or API key) ---
    embedding_provider: str = "local"  # local | openai (openai requires ALLOW_CLOUD_AI)
    openai_embedding_model: str = "text-embedding-3-small"
    local_embedding_dim: int = 256

    # --- Document processing ---
    ocr_enabled: bool = True
    tesseract_cmd: str = "tesseract"
    ocr_lang: str = "eng"
    date_order: str = "MDY"  # how to read ambiguous numeric dates like 05/06/2026 (MDY or DMY); always flagged
    invoice_ai_assist: bool = True  # also ask the model to extract fields (only grounded values accepted)

    # --- QuickBooks Online (read-only) ---
    qbo_mode: str = "mock"  # mock (offline sandbox fixture) | sandbox (live Intuit sandbox) | off
    qbo_fixture: str = "data/qbo_sandbox/sandbox_company.json"
    qbo_client_id: str | None = None
    qbo_client_secret: str | None = None
    qbo_redirect_uri: str = "http://localhost:8000/qbo/callback"
    qbo_allow_production: bool = False  # production company access is out of scope for the prototype
    secrets_backend: str = "file"  # file (0600 in storage/secrets) | keyring (macOS Keychain; pip install keyring)

    # --- Auto-ingest (drop a file into data_dir and it is processed automatically) ---
    auto_reindex: bool = True
    reindex_interval_seconds: int = 5

    # --- Agent loop ---
    max_tool_iterations: int = 5
    prefetch_passages: int = 4  # top passages handed to the model up front (helps small local models)
    max_sql_rows: int = 200
    history_turns: int = 8

    def storage_path(self, *parts: str) -> Path:
        return Path(self.storage_dir, *parts)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
