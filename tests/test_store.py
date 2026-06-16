"""DataStore: safe-query enforcement, limit clamping, and atomic reloads."""

from __future__ import annotations

import duckdb
import pytest

from app.data.store import DataStore, UnsafeQueryError


def test_tables_and_schema(store: DataStore):
    assert "sales" in store.tables()
    schema = store.schema()
    cols = [c for c, _ in schema["sales"]]
    assert {"region", "product", "revenue"} <= set(cols)


def test_run_select_ok(store: DataStore):
    cols, rows = store.run_select("SELECT region, sum(revenue) AS total FROM sales GROUP BY region")
    assert "total" in cols
    totals = {r[0]: r[1] for r in rows}
    assert totals["North"] == 150
    assert totals["South"] == 200


def test_blocks_non_select(store: DataStore):
    with pytest.raises(UnsafeQueryError):
        store.run_select("DROP TABLE sales")


def test_blocks_multiple_statements(store: DataStore):
    with pytest.raises(UnsafeQueryError):
        store.run_select("SELECT 1; DROP TABLE sales")


def test_blocks_file_readers(store: DataStore):
    with pytest.raises(UnsafeQueryError):
        store.run_select("SELECT * FROM read_csv_auto('/etc/passwd')")


def test_blocks_internal_tables(store: DataStore):
    with pytest.raises(UnsafeQueryError):
        store.run_select('SELECT * FROM "_load_sales"')


def test_limit_is_clamped(store: DataStore):
    _, rows = store.run_select("SELECT * FROM sales", max_rows=2)
    assert len(rows) == 2


def test_failed_reload_preserves_data(store: DataStore):
    before = store.run_select("SELECT count(*) AS n FROM sales")[1][0][0]
    with pytest.raises((FileNotFoundError, duckdb.Error)):
        store.load_csv("sales", "/nonexistent/path/does_not_exist.csv")
    after = store.run_select("SELECT count(*) AS n FROM sales")[1][0][0]
    assert before == after == 3
