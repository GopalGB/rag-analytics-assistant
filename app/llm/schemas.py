"""Typed contracts for everything that crosses the model boundary ("type-safe AI").

Every model OUTPUT the app relies on (routing decisions, invoice fields) and every model-issued TOOL
CALL is parsed into one of these Pydantic models before it is used. Invalid output never reaches
business logic: it is rejected, explained back to the model, and retried (see structured.py).
Tool JSON schemas sent to providers are generated from the same models, so the contract the model
sees and the one the code enforces cannot drift apart.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_NULLISH = re.compile(r"^\s*(null|none|n/?a|-+|unknown|not (provided|found|available|given|stated|shown)\b.*)?\s*$", re.I)


def _nullish(v: Any) -> Any:
    return None if isinstance(v, str) and _NULLISH.match(v) else v


# --------------------------------------------------------------------------- tool arguments
# A tool result handed back to the model is cut to this many characters (every tool turn resends it).
TOOL_RESULT_CHARS = 8000


class RunSqlArgs(BaseModel):
    """Run ONE read-only DuckDB SELECT over the available tables and return rows. No DDL/DML, no
    file-reading functions, single statement only."""

    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=5000, description="A single DuckDB SELECT statement.")


class SearchDocsArgs(BaseModel):
    """Retrieve the most relevant document passages (with file and page) for a natural-language query."""

    model_config = ConfigDict(extra="ignore")
    query: str = Field(min_length=1, max_length=500, description="What to look for in the documents.")
    k: int = Field(default=5, description="How many passages (1-10, default 5).")

    @field_validator("k", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> int:
        try:
            return max(1, min(int(v), 10))
        except (TypeError, ValueError):
            return 5


class ProposeActionArgs(BaseModel):
    """Queue an external action (sending an email, recording a bill in QuickBooks, a follow-up task) for
    a person to approve. Nothing is executed; use this only when the user asks for such an action."""

    model_config = ConfigDict(extra="ignore")
    action_type: Literal["draft_email", "record_bill", "follow_up_task", "other"]
    title: str = Field(min_length=1, max_length=200, description="Short summary of the action.")
    details: str = Field(default="", max_length=4000, description="Full draft / details for the approver.")


class AttentionArgs(BaseModel):
    """List what needs attention now, ranked: reconciliation problems, invoices to review, overdue money,
    bank mismatches, budget overruns, overdue or near deadlines from the documents, approval-policy checks
    and unusual bills. Each item has its source. Use it for "what should I look at / prioritise" questions."""

    model_config = ConfigDict(extra="ignore")
    limit: int = Field(default=12, description="How many items (1-30, default 12).")

    @field_validator("limit", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> int:
        try:
            return max(1, min(int(v), 30))
        except (TypeError, ValueError):
            return 12


TOOL_ARGS: dict[str, type[BaseModel]] = {
    "run_sql": RunSqlArgs,
    "search_docs": SearchDocsArgs,
    "propose_action": ProposeActionArgs,
    "attention_items": AttentionArgs,
}


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Provider-friendly JSON Schema for a model (no titles; description kept)."""
    schema = _strip_titles(model.model_json_schema())
    schema.pop("description", None)
    return schema


def tool_spec(name: str) -> dict[str, Any]:
    """OpenAI-style function tool definition generated from the argument model."""
    model = TOOL_ARGS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": " ".join((model.__doc__ or "").split()),
            "parameters": json_schema(model),
        },
    }


# --------------------------------------------------------------------------- model outputs
Intent = Literal["documents", "accounting", "drafting", "general"]


class RouteDecision(BaseModel):
    """Classification of a user request, used by the task router when keyword rules are unsure."""

    model_config = ConfigDict(extra="ignore")
    intent: Intent = Field(
        description="documents = questions about contracts/policies/project records; accounting = figures "
        "from invoices, QuickBooks, bank or spreadsheets; drafting = write an email/letter/report or take an "
        "action; general = anything else."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=300)


class RerankResult(BaseModel):
    """Passage indices, most relevant first."""

    model_config = ConfigDict(extra="ignore")
    order: list[int] = Field(max_length=50)


class InvoiceFields(BaseModel):
    """Header fields printed on a supplier invoice. Use null for anything not printed."""

    model_config = ConfigDict(extra="ignore")
    supplier: str | None = Field(default=None, max_length=200, description="Business that ISSUED the invoice")
    invoice_number: str | None = Field(default=None, max_length=60)
    invoice_date: str | None = Field(default=None, description="YYYY-MM-DD")
    due_date: str | None = Field(default=None, description="YYYY-MM-DD")
    po_number: str | None = Field(default=None, max_length=60)
    currency: str | None = Field(default=None, max_length=3, description="ISO 4217 code")
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _nulls(cls, v: Any) -> Any:
        return _nullish(v)

    @field_validator("subtotal", "tax", "total", mode="before")
    @classmethod
    def _money(cls, v: Any) -> Any:
        if isinstance(v, str):
            cleaned = re.sub(r"[^\d.\-]", "", v)
            return float(cleaned) if cleaned not in ("", "-", ".") else None
        return v

    @field_validator("supplier", "invoice_number", "invoice_date", "due_date", "po_number", "currency", mode="before")
    @classmethod
    def _text(cls, v: Any) -> Any:
        return str(v).strip() if isinstance(v, (int, float)) else v


class CallRecord(BaseModel):
    """One attempt by the model router (for the trace shown to users and the activity log)."""

    model: str
    local: bool
    ok: bool
    ms: int
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
