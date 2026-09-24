"""Workspace: owns every component and runs the end-to-end pipeline.

    files in data dir ──► parse (PDF / Word / text, local OCR for scans) ──► chunk + embed ──► search
                     └──► spreadsheets ──► SQL tables
                     └──► invoices ──► field extraction + checks ──► `invoices` table ──► human review
    QuickBooks (read-only) ──► `qbo_*` tables ──► reconciliation against invoices ──► discrepancies
    questions ──► guardrails ──► task router ──► privacy router ──► model router (fallback chain)
              ──► model + typed tools (or extractive fallback) ──► citation check ──► cited answer
    external actions ──► approval queue (a person decides; nothing executes in this prototype)
    everything above ──► hash-chained activity log
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.accounting import qbo_sync, reconcile, reports
from app.agent.engine import AgentEngine
from app.agent.memory import ConversationMemory
from app.approvals import ApprovalQueue
from app.audit import AuditLog
from app.config import Settings
from app.data import watcher
from app.data.store import DataStore
from app.documents.ocr import OCREngine
from app.documents.parsers import DOC_SUFFIXES, ParseCache
from app.integrations.quickbooks import MockQuickBooks, QuickBooksOnline, TokenStore
from app.invoices.registry import InvoiceRegistry, is_invoice_document
from app.llm.intent import IntentRouter
from app.llm.privacy import PrivacyPolicy, PrivacyRouter
from app.llm.registry import PROVIDERS, build_chain
from app.llm.router import ModelRouter, parse_pricing
from app.rag.embeddings import EmbeddingService
from app.rag.retriever import Retriever
from app.security import InputGuard
from app.security.output_filter import register_secret

UPLOAD_SUFFIXES = DOC_SUFFIXES | {".csv", ".xlsx"}


class LLMNotConfiguredError(RuntimeError):
    """Raised at startup when require_llm is set but no provider is available."""


class Workspace:
    def __init__(self, settings: Settings):
        self.settings = settings
        storage = Path(settings.storage_dir)
        storage.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(storage / "audit.jsonl")
        self.approvals = ApprovalQueue(storage / "approvals.json")
        self.ocr = OCREngine(settings.ocr_enabled, settings.tesseract_cmd, settings.ocr_lang)
        self.parse_cache = ParseCache(storage / "cache" / "parsed")
        self.invoices = InvoiceRegistry(storage / "invoice_reviews.json", date_order=settings.date_order)
        self._lock = threading.RLock()
        self.last_qbo_sync: str | None = None
        self.last_reindex: str | None = None

        for field_name in ("anthropic_api_key", "openai_api_key", "gemini_api_key", "openrouter_api_key", "groq_api_key",
                           "mistral_api_key", "deepseek_api_key", "together_api_key", "xai_api_key",
                           "azure_openai_api_key", "qbo_client_secret", "app_api_key"):
            register_secret(getattr(settings, field_name, None))
        self.router = self._build_router()
        self.llm = self.router.primary
        self.llm_note = self._router_note()
        self.privacy = PrivacyRouter(PrivacyPolicy.from_settings(settings))
        if self.llm is None and settings.require_llm:
            raise LLMNotConfiguredError(f"REQUIRE_LLM is set but no AI model is available: {self.llm_note}")

        embed_provider = settings.embedding_provider
        if embed_provider == "openai" and not settings.allow_cloud_ai:
            embed_provider = "local"  # cloud embeddings would send document text off the machine
        embeddings = EmbeddingService(
            provider=embed_provider,
            dim=settings.local_embedding_dim,
            openai_api_key=settings.openai_api_key,
            openai_base_url=settings.openai_base_url,
            openai_model=settings.openai_embedding_model,
        )
        self.store = DataStore(settings.db_path)
        self.engine = AgentEngine(
            store=self.store,
            retriever=Retriever(embeddings),
            guard=InputGuard(max_input_chars=settings.max_input_chars),
            llm=None,
            router=self.router,
            intents=IntentRouter(model_fallback=settings.intent_model_fallback),
            privacy=self.privacy,
            memory=ConversationMemory(max_turns=settings.history_turns),
            max_tool_iterations=settings.max_tool_iterations,
            max_sql_rows=settings.max_sql_rows,
            approvals=self.approvals,
            on_event=self.audit.record,
            prefetch_passages=settings.prefetch_passages,
        )
        self.qbo = self._build_qbo()
        self.audit.record(
            "app.started",
            actor="system",
            model=getattr(self.llm, "name", None),
            model_local=getattr(self.llm, "is_local", None),
            tiers=self.router.describe()["tiers"],
            cloud_ai_allowed=settings.allow_cloud_ai,
            cloud_allowed_data=sorted(self.privacy.policy.cloud_allowed),
            qbo_mode=settings.qbo_mode,
            ocr=self.ocr.name,
        )

    # ---- AI models -------------------------------------------------------
    def _build_router(self) -> ModelRouter:
        s = self.settings
        fast, notes_fast = build_chain(s, "fast")
        strong, notes_strong = build_chain(s, "strong")
        return ModelRouter(
            {"fast": fast, "strong": strong},
            notes=list(dict.fromkeys(notes_fast + notes_strong)),
            failure_threshold=s.router_failure_threshold,
            cooldown_seconds=s.router_cooldown_seconds,
            pricing=parse_pricing(s.llm_pricing),
        )

    def _router_note(self) -> str:
        r = self.router
        if not r.available:
            blocked = [n for n in r.notes if "explicit approval" in n]
            if blocked:
                return blocked[0]
            return "No AI model configured. Add an API key (docs/LLM-ROUTING.md) or install Ollama for a local model."
        tiers = r.describe()["tiers"]
        return (f"{len(r.models())} model(s). Strong: {' → '.join(tiers['strong']) or '-'}. "
                f"Fast: {' → '.join(tiers['fast']) or '-'}.")

    def router_view(self) -> dict[str, Any]:
        s = self.settings
        pol = self.privacy.policy
        return {
            **self.router.describe(),
            "note": self.llm_note,
            "providers": [
                {"provider": name, "label": info.label,
                 "configured": bool(getattr(s, info.key_setting, None)) if info.key_setting else None}
                for name, info in PROVIDERS.items()
            ],
            "privacy": {
                "cloud_ai_allowed": pol.allow_cloud,
                "cloud_allowed_data": sorted(pol.cloud_allowed),
                "redact_pii": pol.redact_pii,
                "local_model_available": self.router.has_local(),
            },
        }

    # ---- QuickBooks ----------------------------------------------------
    def _build_qbo(self):
        s = self.settings
        if s.qbo_mode == "mock":
            return MockQuickBooks(s.qbo_fixture)
        if s.qbo_mode in ("sandbox", "production"):
            store = TokenStore(self.settings.storage_path("secrets", "qbo_tokens.json"), s.secrets_backend)
            return QuickBooksOnline(
                s.qbo_client_id, s.qbo_client_secret, s.qbo_redirect_uri, store,
                environment=s.qbo_mode, allow_production=s.qbo_allow_production,
            )
        return None

    def qbo_status(self) -> dict[str, Any]:
        if self.qbo is None:
            return {"mode": "off", "connected": False}
        out: dict[str, Any] = {
            "mode": self.qbo.mode,
            "connected": self.qbo.connected,
            "realm_id": getattr(self.qbo, "realm_id", None),
            "last_sync": self.last_qbo_sync,
            "read_only": True,
        }
        if isinstance(self.qbo, QuickBooksOnline):
            out["configured"] = self.qbo.configured
        if "qbo_company" in self.store.tables():
            _, rows = self.store.run_select("SELECT company_name FROM qbo_company", max_rows=1)
            out["company"] = rows[0][0] if rows else None
        return out

    def sync_qbo(self, actor: str = "local-user") -> dict[str, Any]:
        if self.qbo is None or not self.qbo.connected:
            raise PermissionError("QuickBooks is not connected")
        with self._lock:
            counts = qbo_sync.sync(self.qbo, self.store)
            self.last_qbo_sync = datetime.now().isoformat(timespec="seconds")
            # Known vendor names improve supplier matching; re-extract only if new vendors appeared.
            self._rebuild_invoices()
            recon = self._reconcile()
        self.audit.record("qbo.synced", actor=actor, mode=self.qbo.mode, counts=counts, read_only=True)
        return {"counts": counts, "reconciliation_rows": recon, "last_sync": self.last_qbo_sync}

    def _known_vendors(self) -> list[str]:
        if "qbo_vendors" not in self.store.tables():
            return []
        _, rows = self.store.run_select("SELECT name FROM qbo_vendors", max_rows=5000)
        return [r[0] for r in rows if r[0]]

    def _reconcile(self) -> int:
        if "qbo_bills" not in self.store.tables():
            return 0
        rows = reconcile.reconcile(self.invoices.records, self.store)
        return reconcile.load_reconciliation(self.store, rows)

    # ---- indexing --------------------------------------------------------
    def _rebuild_invoices(self) -> None:
        llm = None
        if self.settings.invoice_ai_assist:
            # Invoice text goes to a cloud model only if the privacy policy allows invoice data.
            local_only = not self.privacy.policy.cloud_ok_for("invoices")
            llm = self.router.bound("fast", local_only=local_only, purpose="invoice_extraction")
        self.invoices.build(self.engine.documents, llm=llm, known_suppliers=self._known_vendors())
        self.store.load_dataframe("invoices", self.invoices.dataframe())

    def reindex(self, actor: str = "system") -> dict[str, Any]:
        with self._lock:
            before = {d.file for d in self.engine.documents}
            summary = watcher.reindex(self.engine, self.settings.data_dir, self.ocr, self.parse_cache)
            self._rebuild_invoices()
            self._reconcile()
            self.last_reindex = datetime.now().isoformat(timespec="seconds")
        added = sorted({d.file for d in self.engine.documents} - before)
        summary["invoices"] = len(self.invoices.records)
        summary["new_files"] = added
        if added or actor != "system":
            self.audit.record("documents.indexed", actor=actor, new_files=added[:50], **{
                k: summary[k] for k in ("documents", "doc_chunks", "invoices")
            })
        return summary

    def startup(self) -> None:
        self.reindex()
        if self.qbo is not None and self.qbo.connected and self.settings.qbo_mode == "mock":
            self.sync_qbo(actor="system")

    # ---- uploads -----------------------------------------------------------
    def save_upload(self, filename: str, data: bytes, kind: str = "auto", actor: str = "local-user") -> dict[str, Any]:
        name = Path(filename or "").name
        suffix = Path(name).suffix.lower()
        if suffix not in UPLOAD_SUFFIXES:
            raise ValueError(f"Unsupported file type '{suffix or '?'}'. Allowed: {', '.join(sorted(UPLOAD_SUFFIXES))}")
        if not data:
            raise ValueError("The file is empty.")
        if len(data) > self.settings.max_upload_bytes:
            raise ValueError("The file is too large.")
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._") or "upload"
        if kind == "auto":
            kind = "table" if suffix in (".csv", ".xlsx") else "auto"
        folder = {"invoice": "invoices", "document": "documents", "table": "tables"}.get(kind, "inbox")
        target_dir = Path(self.settings.data_dir, self.settings.upload_subdir, folder)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{stem}{suffix}"
        n = 1
        while target.exists():
            n += 1
            target = target_dir / f"{stem}_{n}{suffix}"
        target.write_bytes(data)
        rel = target.relative_to(Path(self.settings.data_dir)).as_posix()
        self.audit.record("document.uploaded", actor=actor, file=rel, bytes=len(data), kind=kind)
        self.reindex(actor=actor)
        doc = next((d for d in self.engine.documents if d.file == rel), None)
        rec = next((r for r in self.invoices.records if r.file == rel), None)
        return {
            "file": rel,
            "classified_as": "invoice" if rec else ("table" if kind == "table" else "document"),
            "pages": len(doc.pages) if doc else 0,
            "ocr": bool(doc and doc.used_ocr),
            "warnings": doc.warnings if doc else [],
            "invoice": rec.to_dict() if rec else None,
        }

    # ---- views -------------------------------------------------------------
    def documents_view(self) -> list[dict[str, Any]]:
        inv_files = {r.file: r for r in self.invoices.records}
        out = []
        for d in self.engine.documents:
            out.append(
                {
                    "file": d.file,
                    "type": d.kind,
                    "category": "invoice" if d.file in inv_files or is_invoice_document(d) else "document",
                    "pages": len(d.pages),
                    "characters": len(d.text),
                    "ocr_pages": d.ocr_pages,
                    "warnings": d.warnings,
                }
            )
        for table in sorted(self.store.file_tables):
            _, rows = self.store.run_select(f'SELECT count(*) FROM "{table}"', max_rows=1)
            out.append({"file": table, "type": "table", "category": "spreadsheet", "pages": None,
                        "characters": None, "rows": rows[0][0], "ocr_pages": [], "warnings": []})
        return out

    def summary_report(self) -> dict[str, Any]:
        s = reports.build_summary(self.store)
        s["markdown"] = reports.to_markdown(s)
        return s

    def privacy_report(self) -> dict[str, Any]:
        """What runs where, and what (if anything) leaves this machine — derived from live config."""
        models = self.router.models()
        local_models = [m.name for m in models if getattr(m, "is_local", False)]
        cloud_models = [m.name for m in models if not getattr(m, "is_local", False)]
        pol = self.privacy.policy
        allowed = ", ".join(sorted(pol.cloud_allowed)) or "nothing"
        rows = [
            {
                "function": "Document storage, search index and database",
                "runs": "local",
                "internet": False,
                "leaves_machine": "Nothing",
            },
            {
                "function": "Embeddings (search vectors)",
                "runs": "local",
                "internet": self.engine.retriever.embeddings.provider == "openai",
                "leaves_machine": "Nothing" if self.engine.retriever.embeddings.provider != "openai"
                else "Document text sent to the embedding API (approved via ALLOW_CLOUD_AI)",
            },
            {
                "function": f"OCR for scanned documents ({self.ocr.name})",
                "runs": "local",
                "internet": False,
                "leaves_machine": "Nothing",
            },
            {
                "function": "Local AI models: " + (", ".join(local_models) or "none running"),
                "runs": "local",
                "internet": False,
                "leaves_machine": "Nothing",
            },
            {
                "function": "Cloud AI models: " + (", ".join(cloud_models) or "none (blocked or not configured)"),
                "runs": "cloud" if cloud_models else "-",
                "internet": bool(cloud_models),
                "leaves_machine": (
                    f"Only requests whose data class is allowed ({allowed}): the question, relevant passages "
                    f"and tool results{', with emails/phones/account numbers masked' if pol.redact_pii else ''}. "
                    "Accounting, invoice and bank data, and anything with high-risk identifiers, is routed to a "
                    "local model instead" if cloud_models else "Nothing"
                ),
            },
            {
                "function": f"QuickBooks ({self.qbo.mode if self.qbo else 'off'}, read-only)",
                "runs": "local fixture" if (self.qbo is None or self.qbo.mode == "mock") else "Intuit API",
                "internet": self.qbo is not None and self.qbo.mode != "mock",
                "leaves_machine": "Nothing" if (self.qbo is None or self.qbo.mode == "mock")
                else "OAuth sign-in and read-only queries to Intuit; accounting data is downloaded, never uploaded",
            },
            {
                "function": "Email",
                "runs": "not connected in this prototype",
                "internet": False,
                "leaves_machine": "Nothing",
            },
        ]
        return {
            "model_note": self.llm_note,
            "cloud_ai_allowed": self.settings.allow_cloud_ai,
            "functions": rows,
            "controls": [
                "Server listens on 127.0.0.1 only by default; optional API key for any other access.",
                "External actions (emails, accounting changes) require named approval and are not executed.",
                "QuickBooks access is read-only in code (GET queries on an allowlist); tokens can be revoked.",
                "Every question, upload, review, sync and approval is written to a hash-chained activity log.",
                "No data is used for model training: local models run offline; cloud use is off unless approved.",
                "Privacy router: sensitive data classes and high-risk identifiers are only ever sent to local models; "
                "the tool layer blocks cloud models from reading them and withholds earlier local-only turns.",
                "Encryption at rest: keep the data and storage folders on a FileVault-encrypted disk.",
            ],
        }
