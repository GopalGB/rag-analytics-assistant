"""DataStore: safe-query enforcement, limit clamping, and atomic reloads."""

from __future__ import annotations

import duckdb
import pandas as pd
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


def test_blocks_unbounded_table_functions(store: DataStore):
    with pytest.raises(UnsafeQueryError):
        store.run_select("SELECT sum(i) FROM range(1000000000000) AS t(i)")


def test_blocks_other_table_functions_from_the_parsed_query(store: DataStore):
    with pytest.raises(UnsafeQueryError, match="table function"):
        store.run_select("SELECT * FROM query_table('sales')")


def test_blocks_recursive_queries_before_execution(store: DataStore):
    with pytest.raises(UnsafeQueryError, match="recursive"):
        store.run_select(
            "WITH RECURSIVE counter(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM counter WHERE n < 100) SELECT * FROM counter"
        )


def test_query_timeout_interrupts_and_allows_the_next_query(tmp_path):
    store = DataStore(str(tmp_path / "timeout.duckdb"), query_timeout_seconds=0.001)
    try:
        store.load_dataframe("work", pd.DataFrame({"n": range(100)}))
        with pytest.raises(UnsafeQueryError, match="time limit"):
            store.run_select(
                "SELECT sum(sin(a.n * b.n * c.n * d.n)) FROM work a CROSS JOIN work b CROSS JOIN work c CROSS JOIN work d"
            )
        assert store.run_select("SELECT count(*) AS n FROM work")[1] == [(100,)]
    finally:
        store.close()


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


def test_load_rejects_malicious_table_name_without_changing_existing_table(store: DataStore):
    before = store.run_select("SELECT count(*) AS n FROM sales")[1]
    with pytest.raises(UnsafeQueryError, match="invalid table name"):
        store.load_dataframe('sales"; DROP TABLE sales; --', pd.DataFrame({"region": ["West"]}))
    assert store.run_select("SELECT count(*) AS n FROM sales")[1] == before


@pytest.mark.parametrize("expression", ["getenv('OPENAI_API_KEY')", "current_setting('search_path')"])
def test_blocks_environment_and_setting_introspection(store: DataStore, monkeypatch, expression: str):
    monkeypatch.setenv("OPENAI_API_KEY", "gsk_synthetic_nonsecret_sentinel")
    with pytest.raises(UnsafeQueryError):
        store.run_select(f"SELECT {expression}")
