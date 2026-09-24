"""Provider catalogue and model specs.

A model is named by a spec `provider:model`, e.g.

    anthropic:claude-sonnet-5        openai:gpt-4o-mini         gemini:gemini-2.5-flash
    openrouter:meta-llama/llama-3.3-70b-instruct                groq:openai/gpt-oss-120b
    mistral:mistral-large-latest     deepseek:deepseek-chat     together:<model>   xai:<model>
    azure:<deployment-name>          bedrock:<model-id>         ollama:qwen2.5:14b  cli:default

Add a provider by setting its API key in `.env`; the default fast/strong models below are used unless
LLM_MODELS_FAST / LLM_MODELS_STRONG list specs explicitly (comma-separated = fallback order).

Cloud models are only built when ALLOW_CLOUD_AI=true; otherwise they are skipped with a note.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.llm.providers import (
    AnthropicLLM,
    AzureOpenAILLM,
    BaseLLM,
    BedrockLLM,
    CommandLLM,
    OllamaLLM,
    OpenAICompatLLM,
    OpenAILLM,
)


@dataclass(frozen=True)
class ProviderInfo:
    key_setting: str | None  # Settings field holding the API key
    fast: str | None  # default model for the fast tier (None = must be set explicitly)
    strong: str | None
    base_url: str | None = None  # OpenAI-compatible base URL
    json_mode: str = "json_object"
    label: str = ""


# Defaults are conveniences. Model names change: set LLM_MODELS_FAST / LLM_MODELS_STRONG to the exact
# models your account has access to.
PROVIDERS: dict[str, ProviderInfo] = {
    "anthropic": ProviderInfo("anthropic_api_key", "claude-haiku-4-5-20251001", "claude-sonnet-5", label="Anthropic Claude"),
    "openai": ProviderInfo("openai_api_key", "gpt-4o-mini", "gpt-4o", json_mode="json_schema", label="OpenAI"),
    "gemini": ProviderInfo("gemini_api_key", "gemini-2.5-flash", "gemini-2.5-pro",
                           "https://generativelanguage.googleapis.com/v1beta/openai", label="Google Gemini"),
    "openrouter": ProviderInfo("openrouter_api_key", "openai/gpt-4o-mini", "openai/gpt-4o",
                               "https://openrouter.ai/api/v1", label="OpenRouter"),
    "azure": ProviderInfo("azure_openai_api_key", None, None, label="Azure OpenAI (model = deployment name)"),
    "groq": ProviderInfo("groq_api_key", "openai/gpt-oss-20b", "openai/gpt-oss-120b",
                         "https://api.groq.com/openai/v1", label="Groq"),
    "mistral": ProviderInfo("mistral_api_key", "mistral-small-latest", "mistral-large-latest",
                            "https://api.mistral.ai/v1", label="Mistral"),
    "deepseek": ProviderInfo("deepseek_api_key", "deepseek-chat", "deepseek-chat", "https://api.deepseek.com/v1",
                             label="DeepSeek"),
    "together": ProviderInfo("together_api_key", None, None, "https://api.together.xyz/v1", label="Together AI"),
    "xai": ProviderInfo("xai_api_key", None, None, "https://api.x.ai/v1", label="xAI"),
    "bedrock": ProviderInfo(None, None, None, label="AWS Bedrock"),
    "ollama": ProviderInfo(None, None, None, label="Ollama (local)"),
    "cli": ProviderInfo(None, None, None, label="Command-line model"),
}
CLOUD_ORDER = ["anthropic", "openai", "gemini", "openrouter", "azure", "groq", "mistral", "deepseek", "together", "xai",
               "bedrock"]

CLOUD_BLOCKED = (
    "{what} would send questions and document extracts off this machine, which needs explicit approval. "
    "Set ALLOW_CLOUD_AI=true once approved, or use a local model (Ollama)."
)


class SpecError(ValueError):
    pass


def parse_spec(spec: str) -> tuple[str, str]:
    provider, sep, model = spec.strip().partition(":")
    provider = provider.strip().lower()
    if not sep or provider not in PROVIDERS:
        raise SpecError(f"invalid model spec {spec!r}: use provider:model with provider in {sorted(PROVIDERS)}")
    return provider, model.strip()


def parse_specs(value: str | None) -> list[str]:
    return [s.strip() for s in (value or "").split(",") if s.strip()]


_probe_cache: dict[str, tuple[float, bool]] = {}


def ollama_reachable(base_url: str) -> bool:
    hit = _probe_cache.get(base_url)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    ok = OllamaLLM.reachable(base_url)
    _probe_cache[base_url] = (time.time(), ok)
    return ok


def build_model(spec: str, settings: Any, http: Any = None) -> tuple[BaseLLM | None, str]:
    """Instantiate one model from a spec. Returns (llm or None, note)."""
    try:
        provider, model = parse_spec(spec)
    except SpecError as exc:
        return None, str(exc)
    info = PROVIDERS[provider]
    t = settings.llm_timeout
    allow_cloud = settings.allow_cloud_ai

    def gate(llm: BaseLLM) -> tuple[BaseLLM | None, str]:
        if not llm.is_local and not allow_cloud:
            return None, CLOUD_BLOCKED.format(what=f"{llm.name} (cloud)")
        return llm, f"{llm.name} ({'local' if llm.is_local else 'cloud'})"

    if provider == "ollama":
        if not model:
            return None, "ollama: no model given"
        return gate(OllamaLLM(settings.ollama_base_url, model, t, http=http))
    if provider == "cli":
        if not settings.llm_cli_command:
            return None, "LLM_CLI_COMMAND not set."
        return gate(CommandLLM(settings.llm_cli_command, settings.llm_cli_timeout, is_local=settings.llm_cli_is_local))
    if provider == "bedrock":
        model = model or settings.bedrock_model_id or ""
        if not model:
            return None, "BEDROCK_MODEL_ID not set."
        if not allow_cloud:
            return None, CLOUD_BLOCKED.format(what="AWS Bedrock")
        return BedrockLLM(model, settings.aws_region), f"bedrock:{model} (cloud)"

    key = getattr(settings, info.key_setting or "", None)
    if not key:
        return None, f"{provider}: {info.key_setting.upper()} not set."
    if not model:
        return None, f"{provider}: no model given"
    if provider == "anthropic":
        return gate(AnthropicLLM(key, model, settings.anthropic_base_url, t, http=http))
    if provider == "azure":
        if not settings.azure_openai_endpoint:
            return None, "azure: AZURE_OPENAI_ENDPOINT not set."
        return gate(AzureOpenAILLM(key, settings.azure_openai_endpoint, model, settings.azure_openai_api_version, t, http=http))
    if provider == "openai":
        return gate(OpenAILLM(key, settings.openai_base_url, model, t, http=http))
    return gate(OpenAICompatLLM(key, info.base_url or "", model, provider=provider, timeout=t,
                                json_mode=info.json_mode, http=http))


def _configured_cloud(settings: Any) -> list[str]:
    out = []
    for p in CLOUD_ORDER:
        info = PROVIDERS[p]
        if p == "bedrock":
            if settings.bedrock_model_id:
                out.append(p)
        elif getattr(settings, info.key_setting or "", None):
            out.append(p)
    return out


def default_chain(settings: Any, tier: str) -> list[str]:
    """Fallback order for a tier when LLM_MODELS_<TIER> isn't set."""
    provider = settings.llm_provider
    local: list[str] = []
    ollama_model = settings.ollama_model if tier == "fast" else (settings.ollama_model_strong or settings.ollama_model)
    if ollama_model and (provider == "ollama" or ollama_reachable(settings.ollama_base_url)):
        local.append(f"ollama:{ollama_model}")

    def cloud_spec(p: str) -> str | None:
        info = PROVIDERS[p]
        if p == "openai":
            return f"openai:{settings.openai_model if tier == 'fast' else (settings.openai_model_strong or settings.openai_model)}"
        if p == "bedrock":
            return f"bedrock:{settings.bedrock_model_id}"
        model = info.fast if tier == "fast" else info.strong
        return f"{p}:{model}" if model else None

    if provider == "none":
        return []
    if provider not in ("auto", ""):
        if provider == "ollama":
            return local or ([f"ollama:{ollama_model}"] if ollama_model else [])
        if provider == "cli":
            return ["cli:default"]
        spec = cloud_spec(provider) if provider in PROVIDERS else None
        return [spec] if spec else []
    cloud = [s for s in (cloud_spec(p) for p in _configured_cloud(settings)) if s]
    cli = ["cli:default"] if settings.llm_cli_command else []
    chain = (local + cloud + cli) if settings.llm_prefer == "local" else (cloud + cli + local)
    return list(dict.fromkeys(chain))


def build_chain(settings: Any, tier: str, http: Any = None) -> tuple[list[BaseLLM], list[str]]:
    specs = parse_specs(getattr(settings, f"llm_models_{tier}", None)) or default_chain(settings, tier)
    models, notes = [], []
    for spec in specs:
        llm, note = build_model(spec, settings, http=http)
        notes.append(note)
        if llm is not None:
            models.append(llm)
    return models, notes


def select_llm(settings: Any) -> tuple[BaseLLM | None, str]:
    """The primary (first strong-tier) model and a note explaining the choice."""
    models, notes = build_chain(settings, "strong")
    if models:
        return models[0], f"{models[0].name} ({'local' if models[0].is_local else 'cloud'})"
    if settings.llm_provider == "none":
        return None, "AI model disabled (LLM_PROVIDER=none)."
    blocked = [n for n in notes if "explicit approval" in n]
    if blocked:
        return None, blocked[0]
    return None, "No AI model configured. Add an API key (see docs/LLM-ROUTING.md) or install Ollama for a local model."


def build_llm(settings: Any) -> BaseLLM | None:
    return select_llm(settings)[0]
