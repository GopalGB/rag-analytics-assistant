"""Central configuration. All values come from environment variables (or a local .env file).

Field names map case-insensitively to environment variables, e.g. `app_api_key` <- `APP_API_KEY`.
Defaults are chosen for a private, local-first prototype: local model, local embeddings, local OCR,
offline QuickBooks sandbox fixture, no cloud AI unless explicitly approved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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

    # --- Operations ---
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    metrics_enabled: bool = True  # expose /metrics (Prometheus text format)

    # --- HTTP security ---
    app_api_key: str | None = None  # optional shared key; if set, non-public routes require it
    rate_per_minute: int = 60
    rate_burst: int = 20
    max_body_bytes: int = 64 * 1024
    max_input_chars: int = 2000
    # Loopback exemption for the API key. Safe for local use; set False behind a reverse proxy
    # (where every request appears to come from 127.0.0.1) so the key is always required.
    trust_loopback: bool = True

    # --- AI models ---
    # With no model the assistant still works: document questions return the most relevant source
    # passages (extractive mode) and invoices are extracted by local rules. Set True to refuse to
    # start without a model. Full guide: docs/LLM-ROUTING.md
    require_llm: bool = False
    llm_provider: str = "auto"  # auto | <provider> (restrict to one) | none
    llm_prefer: Literal["cloud", "local"] = "cloud"  # when both are configured and allowed: cloud-first or local-first fallback order
    llm_timeout: int = 120
    # Model router: comma-separated `provider:model` specs in fallback order, per tier. Empty = derived
    # from the API keys present (see app/llm/registry.py for per-provider defaults).
    llm_models_fast: str | None = None  # e.g. "anthropic:claude-haiku-4-5-20251001,openai:gpt-4o-mini,ollama:qwen2.5:14b"
    llm_models_strong: str | None = None  # e.g. "anthropic:claude-sonnet-5,openai:gpt-4o,ollama:qwen2.5:32b"
    router_failure_threshold: int = 3  # consecutive failures before a model is skipped...
    router_cooldown_seconds: int = 60  # ...for this long (circuit breaker)
    llm_pricing: str | None = None  # JSON {"provider:model": [usd_per_M_input, usd_per_M_output]} for cost tracking
    intent_model_fallback: bool = True  # ask the fast model when keyword routing is unsure

    # --- Privacy router ---
    # Master switch: cloud AI is refused unless the owner has approved it.
    allow_cloud_ai: bool = False
    # Data classes that MAY be sent to a cloud model (documents, invoices, accounting, bank, personal).
    cloud_allowed_data: str = "documents"
    redact_pii: bool = True  # mask emails / phones / account numbers in anything sent to a cloud model
    sensitive_paths: str = ""  # extra folder rules, e.g. "hr/=personal,payroll/=personal,finance/=accounting"

    # --- Provider keys (add any; only providers with a key are used) ---
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"  # or any OpenAI-compatible server (LM Studio = local)
    openai_model: str = "gpt-4o-mini"  # default fast model for openai
    openai_model_strong: str | None = "gpt-4o"
    gemini_api_key: str | None = None
    openrouter_api_key: str | None = None
    groq_api_key: str | None = None
    mistral_api_key: str | None = None
    deepseek_api_key: str | None = None
    together_api_key: str | None = None
    xai_api_key: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_endpoint: str | None = None  # https://<resource>.openai.azure.com
    azure_openai_api_version: str = "2024-10-21"
    bedrock_model_id: str | None = None  # AWS Bedrock (needs boto3 + AWS credentials in the environment)
    aws_region: str = "us-east-1"
    # Ollama (local, recommended on a Mac Studio): https://ollama.com
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model: str | None = "qwen2.5:14b"
    ollama_model_strong: str | None = None  # e.g. qwen2.5:32b on a 64 GB Mac
    # CLI backend: a shell command that reads the prompt on stdin and writes the completion to stdout.
    llm_cli_command: str | None = None
    llm_cli_timeout: int = 120
    llm_cli_is_local: bool = False  # set True only if the CLI runs a model on this machine

    # --- Search / embeddings ---
    # local  = offline hashing (no model; default fallback)
    # ollama = local semantic model via Ollama (`ollama pull nomic-embed-text`) — recommended
    # openai = any OpenAI-compatible /embeddings API (a localhost/LAN URL counts as local)
    # auto   = ollama if it is running, else local
    embedding_provider: Literal["auto", "local", "ollama", "openai"] = "auto"
    embedding_model: str | None = None  # default: nomic-embed-text (ollama) / text-embedding-3-small (openai)
    embedding_base_url: str | None = None  # default: OLLAMA_BASE_URL (ollama) / OPENAI_BASE_URL (openai)
    embedding_api_key: str | None = None  # default: OPENAI_API_KEY for the openai provider
    embedding_query_prefix: str | None = None  # auto for nomic models: "search_query: "
    embedding_doc_prefix: str | None = None  # auto for nomic models: "search_document: "
    # Cloud embeddings send the text of EVERY document (incl. invoices and bank data) to the provider,
    # so they need this explicit approval on top of ALLOW_CLOUD_AI.
    allow_cloud_embeddings: bool = False
    openai_embedding_model: str = "text-embedding-3-small"  # legacy alias
    local_embedding_dim: int = 256
    rerank_with_model: bool = False  # reorder search results with a LOCAL model (type-safe; slower)

    # --- Document processing ---
    ocr_enabled: bool = True
    tesseract_cmd: str = "tesseract"
    ocr_lang: str = "eng"
    date_order: Literal["MDY", "DMY"] = "MDY"  # how to read ambiguous numeric dates like 05/06/2026 (MDY or DMY); always flagged
    invoice_ai_assist: bool = True  # also ask the model to extract fields (only grounded values accepted)

    # --- QuickBooks Online (read-only) ---
    qbo_mode: Literal["mock", "sandbox", "production", "off"] = "mock"  # mock (offline sandbox fixture) | sandbox (live Intuit sandbox) | off
    qbo_fixture: str = "data/qbo_sandbox/sandbox_company.json"
    qbo_client_id: str | None = None
    qbo_client_secret: str | None = None
    qbo_redirect_uri: str = "http://localhost:8000/qbo/callback"
    qbo_allow_production: bool = False  # production company access is out of scope for the prototype
    secrets_backend: Literal["file", "keyring"] = "file"  # file (0600 in storage/secrets) | keyring (macOS Keychain; pip install keyring)

    # --- Reports / dashboard ---
    report_as_of: str | None = None  # YYYY-MM-DD for aging/overdue figures; default: today

    # --- Auto-ingest (drop a file into data_dir and it is processed automatically) ---
    auto_reindex: bool = True
    reindex_interval_seconds: int = 5

    # --- Agent loop ---
    max_tool_iterations: int = 5
    prefetch_passages: int = 4  # top passages handed to the model up front (helps small local models)
    max_sql_rows: int = 200
    history_turns: int = 8

    def warnings(self) -> list[str]:
        """Risky or inconsistent settings. Logged at startup and shown on /health; never fatal."""
        from datetime import date

        out: list[str] = []
        if not Path(self.data_dir).exists():
            out.append(f"DATA_DIR {self.data_dir!r} does not exist.")
        if not self.trust_loopback and not self.app_api_key:
            out.append("TRUST_LOOPBACK=false but APP_API_KEY is not set: nothing protects the API.")
        if self.allow_cloud_ai and not self.redact_pii:
            out.append("Cloud AI is allowed with REDACT_PII=false: personal details may be sent to cloud models.")
        sensitive = {"accounting", "bank", "personal", "invoices"} & {c.strip() for c in self.cloud_allowed_data.split(",")}
        if self.allow_cloud_ai and sensitive:
            out.append(f"CLOUD_ALLOWED_DATA lets {', '.join(sorted(sensitive))} data go to cloud models. Confirm this is approved.")
        if self.allow_cloud_embeddings:
            out.append("ALLOW_CLOUD_EMBEDDINGS is on: the text of every document may be sent to the embedding provider.")
        if self.qbo_mode in ("sandbox", "production") and not (self.qbo_client_id and self.qbo_client_secret):
            out.append("QBO_MODE needs QBO_CLIENT_ID and QBO_CLIENT_SECRET.")
        if self.report_as_of:
            try:
                date.fromisoformat(self.report_as_of)
            except ValueError:
                out.append(f"REPORT_AS_OF {self.report_as_of!r} is not YYYY-MM-DD; using today.")
        known = {"auto", "none", "anthropic", "openai", "gemini", "openrouter", "azure", "groq", "mistral", "deepseek",
                 "together", "xai", "bedrock", "ollama", "cli"}
        if self.llm_provider not in known:
            out.append(f"LLM_PROVIDER {self.llm_provider!r} is not recognised.")
        return out

    def storage_path(self, *parts: str) -> Path:
        return Path(self.storage_dir, *parts)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
