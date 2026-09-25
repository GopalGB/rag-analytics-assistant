"""Type-safe model calls: `generate(llm, Model, system, prompt) -> Model`.

1. The target Pydantic model's JSON Schema is sent with the request, using the provider's strongest
   constraint (`complete_json`: OpenAI json_schema, JSON mode, or Anthropic tool forcing), and falling
   back to plain completion + instructions for providers without one.
2. The reply is parsed and validated with Pydantic. On failure the exact validation errors are sent
   back to the model and it gets another try (`retries`).
3. If it still doesn't validate, `StructuredOutputError` is raised. Callers fall back to deterministic
   logic — invalid model output never reaches business code.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.schemas import json_schema

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    def __init__(self, message: str, attempts: int, last_raw: str = ""):
        super().__init__(message)
        self.attempts = attempts
        self.last_raw = last_raw


def extract_json(text: str) -> Any:
    """Parse the first JSON object in `text` (tolerates code fences and chatter around it)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON object found in the reply")


def _explain(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['msg']}" for e in exc.errors()[:8])
    return str(exc)


def generate(llm: Any, model: type[T], system: str, prompt: str, retries: int = 2) -> T:
    schema = json_schema(model)
    instructions = (
        f"{system}\n\nReply with ONE JSON object and nothing else. It must validate against this JSON Schema:\n"
        f"{json.dumps(schema)}"
    )
    message, raw = prompt, ""
    for _attempt in range(retries + 1):
        if callable(getattr(llm, "complete_json", None)):
            raw = llm.complete_json(instructions, message, schema, model.__name__)
        else:
            raw = llm.complete(instructions, message)
        try:
            return model.model_validate(extract_json(raw))
        except (ValueError, ValidationError) as exc:
            message = (
                f"{prompt}\n\nYour previous reply was not valid: {_explain(exc)}.\n"
                "Return corrected JSON only, matching the schema exactly."
            )
    raise StructuredOutputError(f"{model.__name__}: no valid output after {retries + 1} attempts", retries + 1, raw)
