"""Auto-ingest: dropping/removing files in the data dir adapts the store + retriever via reindex()."""

from __future__ import annotations

from pathlib import Path

from app.agent.engine import AgentEngine
from app.agent.memory import ConversationMemory
from app.data import ingest, watcher
from app.data.store import DataStore
from app.rag.embeddings import EmbeddingService
from app.rag.retriever import Retriever
from app.security import InputGuard


def _engine(data_dir: Path, db_path: Path) -> AgentEngine:
    store = DataStore(str(db_path))
    ingest.load_tables(store, str(data_dir))
    emb = EmbeddingService(provider="local", dim=64)
    retriever = Retriever(emb).build(ingest.load_chunks(str(data_dir)))
    return AgentEngine(
        store=store,
        retriever=retriever,
        guard=InputGuard(max_input_chars=2000),
        llm=None,
        memory=ConversationMemory(max_turns=2),
    )


def _write_csv(path: Path, header: str, *rows: str) -> None:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def test_dir_signature_changes_on_edit(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _write_csv(d / "sales.csv", "region,revenue", "North,100")
    sig1 = watcher.dir_signature(str(d))
    _write_csv(d / "costs.csv", "region,cost", "North,40")
    sig2 = watcher.dir_signature(str(d))
    assert sig1 != sig2
    assert any(name == "costs.csv" for name, _, _ in sig2)


def test_reindex_picks_up_new_table_and_doc(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _write_csv(d / "sales.csv", "region,revenue", "North,100", "South,200")
    engine = _engine(d, tmp_path / "w.duckdb")
    try:
        assert "costs" not in engine.store.tables()

        # Drop in a brand-new CSV + a new document, then reindex (what the watcher does on change).
        _write_csv(d / "costs.csv", "region,cost", "North,40", "South,90")
        (d / "notes.md").write_text("Margin is revenue minus cost.", encoding="utf-8")
        result = watcher.reindex(engine, str(d))

        assert "costs" in result["tables"]
        assert "costs" in engine.store.tables()
        # The new table is immediately queryable through the safe SELECT path.
        cols, rows = engine.store.run_select("SELECT sum(cost) AS c FROM costs")
        assert rows[0][0] == 130
        # The new document is embedded + indexed and retrievable.
        hits = engine.retriever.search("how is margin calculated", k=3)
        assert any(h.file == "notes.md" for h in hits)
    finally:
        engine.store.close()


def test_reindex_drops_table_when_file_removed(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _write_csv(d / "sales.csv", "region,revenue", "North,100")
    _write_csv(d / "costs.csv", "region,cost", "North,40")
    engine = _engine(d, tmp_path / "w.duckdb")
    try:
        assert "costs" in engine.store.tables()
        (d / "costs.csv").unlink()
        watcher.reindex(engine, str(d))
        assert "costs" not in engine.store.tables()
        assert "sales" in engine.store.tables()  # surviving table untouched
    finally:
        engine.store.close()


def test_reindex_reports_bad_file_without_losing_healthy_documents(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "healthy.md").write_text("Baseline is the normal expected volume.", encoding="utf-8")
    engine = _engine(d, tmp_path / "w.duckdb")
    try:
        (d / "too-large.txt").write_bytes(b"x" * (ingest.MAX_FILE_BYTES + 1))
        result = watcher.reindex(engine, str(d))
        assert result["errors"] == [{"file": "too-large.txt", "error": "source too large: too-large.txt"}]
        assert any(hit.file == "healthy.md" for hit in engine.retriever.search("normal expected volume", k=1))
    finally:
        engine.store.close()
