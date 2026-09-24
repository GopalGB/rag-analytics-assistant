"""Workspace: owns every component and runs the end-to-end pipeline.

    files in data dir ──► parse (PDF / Word / text, local OCR for scans) ──► chunk + embed ──► search
                     └──► spreadsheets ──► SQL tables
                     └──► invoices ──► field extraction + checks ──► `invoices` table ──► human review
    QuickBooks (read-only) ──► `qbo_*` tables ──► reconciliation against invoices ──► discrepancies
    questions ──► guardrails ──► model + tools (or extractive fallback) ──► cited answer
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
from app.agent.llm import select_llm
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
from app.rag.embeddings import EmbeddingService
from app.rag.retriever import Retriever
from app.security import InputGuard

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

        self.llm, self.llm_note = select_llm(settings)
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
            llm=self.llm,
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
            qbo_mode=settings.qbo_mode,
            ocr=self.ocr.name,
        )

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
        llm = self.llm if self.settings.invoice_ai_assist else None
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
        llm = self.llm
        llm_local = bool(llm is not None and getattr(llm, "is_local", False))
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
                "function": "AI model: " + (getattr(llm, "name", None) or "none (extractive mode)"),
                "runs": "local" if (llm is None or llm_local) else "cloud",
                "internet": llm is not None and not llm_local,
                "leaves_machine": "Nothing" if (llm is None or llm_local)
                else "Questions, retrieved passages and query results are sent to the model provider",
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
                "Encryption at rest: keep the data and storage folders on a FileVault-encrypted disk.",
            ],
        }
