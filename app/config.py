"""Central configuration. All values come from environment variables (or a local .env file).

Field names map case-insensitively to environment variables, e.g. `app_api_key` <- `APP_API_KEY`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "RAG Analytics Assistant"
    data_dir: str = str(Path(__file__).resolve().parents[1] / "data" / "sample")
    db_path: str = "storage/analytics.duckdb"

    # --- HTTP security ---
    app_api_key: str | None = None  # optional shared key; if set, non-public routes require it
    rate_per_minute: int = 60
    rate_burst: int = 20
    max_body_bytes: int = 64 * 1024
    max_input_chars: int = 2000
    # Loopback exemption for the API key. Safe for local dev; set False behind a reverse proxy
    # (where every request appears to come from 127.0.0.1) so the key is always required.
    trust_loopback: bool = True
    public_demo: bool = False
    qbo_sandbox_access_token: str | None = None
    qbo_realm_id: str | None = None

    # --- LLM provider (REQUIRED — this is an LLM-first assistant) ---
    # The assistant answers strictly through a live LLM (cloud API or local CLI). There is no
    # deterministic "offline answerer": if require_llm is True (default) and no provider is
    # configured, the app refuses to start with a clear message instead of faking answers.
    require_llm: bool = True
    llm_provider: str = "auto"  # auto | openai | bedrock | cli | none
    # OpenAI (or any OpenAI-compatible /chat/completions endpoint)
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"  # set to whatever model you actually have access to
    # AWS Bedrock (needs boto3 + AWS credentials in the environment)
    bedrock_model_id: str | None = None  # e.g. anthropic.claude-3-5-sonnet-20240620-v1:0
    aws_region: str = "us-east-1"
    # Local CLI backend (e.g. the ChatGPT/Codex CLI): a shell command that reads the prompt on
    # stdin and writes the completion to stdout. Lets you use an existing CLI subscription.
    llm_cli_command: str | None = None
    llm_cli_timeout: int = 120

    # --- Embeddings (vectorization — runs locally by default; not an LLM) ---
    embedding_provider: str = "auto"  # auto | openai | local
    openai_embedding_model: str = "text-embedding-3-small"
    local_embedding_dim: int = 256

    # --- Auto-ingest (drop a file into data_dir and it is embedded + indexed automatically) ---
    auto_reindex: bool = True
    reindex_interval_seconds: int = 5

    # --- Agent loop ---
    max_tool_iterations: int = 4
    max_sql_rows: int = 200
    history_turns: int = 8


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
