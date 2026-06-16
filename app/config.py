"""Central configuration. All values come from environment variables (or a local .env file).

Field names map case-insensitively to environment variables, e.g. `app_api_key` <- `APP_API_KEY`.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "RAG Analytics Assistant"
    data_dir: str = "data/sample"
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

    # --- LLM provider (optional; app runs deterministically without it) ---
    llm_provider: str = "auto"  # auto | openai | none
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"  # set to whatever model you actually have access to

    # --- Embeddings ---
    embedding_provider: str = "auto"  # auto | openai | local
    openai_embedding_model: str = "text-embedding-3-small"
    local_embedding_dim: int = 256

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
