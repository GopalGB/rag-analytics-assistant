"""DuckDB-backed tabular store with read-only query safety and atomic table reloads.

Security model (defense in depth — a denylist of function names is NOT relied upon):
1. LOCKDOWN: the connection runs with `enable_external_access=false`, so no query can read a file
   or the network no matter how it is phrased (direct path `FROM '/etc/x'`, read_csv, read_ndjson,
   read_duckdb, …). This is the primary boundary and cannot be evaded by SQL text tricks.
2. TABLE ALLOWLIST: every query is parsed (DuckDB's own parser) and each referenced table must be a
   loaded, non-internal table (or a CTE defined in the same query). Internal/temp and unknown tables
   are rejected — closing direct-path reads and `main._internal` schema-qualified access.
3. HARD ROW CAP: the query is wrapped in an outer `LIMIT`, so a missing or subquery-only `LIMIT`
   cannot return (or materialize) unbounded rows.

Ingestion reads CSVs in Python (pandas) and registers them in memory, so the engine never needs
filesystem access even while loading data.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import duckdb
import pandas as pd

# Introspection/table functions that expose engine internals. File-reading functions are already
# impossible (lockdown); this is belt-and-suspenders against metadata disclosure.
_BLOCKED_TOKENS = ("duckdb_", "pragma_", "sqlite_", "information_schema", "pg_catalog", "glob(", "getenv", "current_setting")
_UNBOUNDED_TABLE_FUNCTIONS = re.compile(r"\b(?:range|generate_series|unnest)\s*\(", re.I)
_RECURSIVE_QUERY = re.compile(r"\bwith\s+recursive\b", re.I)
_QUERY_TIMEOUT_SECONDS = 2.0

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"--[^\n]*")


class UnsafeQueryError(ValueError):
    """Raised when a query is not a single, safe, read-only SELECT over allowed tables."""


class DataStore:
    """Owns one DuckDB connection. Thread-safe via a coarse re-entrant lock."""

    def __init__(self, db_path: str, query_timeout_seconds: float = _QUERY_TIMEOUT_SECONDS):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(db_path)
        # Hard lockdown: queries may not touch the filesystem or network. One-way; never re-enabled.
        self.con.execute("SET enable_external_access=false")
        self.con.execute("SET memory_limit='256MB'")
        self._lock = threading.RLock()
        self._table_sources: dict[str, str] = {}
        self._query_timeout_seconds = float(query_timeout_seconds)
        if self._query_timeout_seconds <= 0:
            raise ValueError("query_timeout_seconds must be positive")

    # ---- introspection -------------------------------------------------
    def tables(self) -> list[str]:
        with self._lock:
            rows = self.con.execute("SHOW TABLES").fetchall()
        return [t for (t,) in rows if not str(t).startswith("_")]

    def schema(self) -> dict[str, list[tuple[str, str]]]:
        out: dict[str, list[tuple[str, str]]] = {}
        with self._lock:
            for table in self.tables():
                cols = self.con.execute(f'PRAGMA table_info("{table}")').fetchall()
                out[table] = [(c[1], c[2]) for c in cols]
        return out

    def schema_summary(self, max_cols: int = 40) -> str:
        lines: list[str] = []
        for table, cols in self.schema().items():
            shown = cols[:max_cols]
            col_text = ", ".join(f"{name} {ctype}" for name, ctype in shown)
            if len(cols) > max_cols:
                col_text += f", … (+{len(cols) - max_cols} more)"
            lines.append(f'- "{table}"({col_text})')
        return "\n".join(lines)

    # ---- ingestion (pandas in-memory; the engine never reads files) ----
    def load_csv(self, table: str, path: str) -> int:
        return self.load_dataframe(table, pd.read_csv(path), source_file=Path(path).name)

    def drop_table(self, table: str) -> None:
        """Drop a loaded table (used when its source file is removed from the data dir).

        The name is validated against a strict identifier pattern — drops are only ever invoked with
        names produced by ingestion, never with model- or user-supplied text."""
        if not re.fullmatch(r"[A-Za-z0-9_]+", table) or table.startswith("_"):
            raise UnsafeQueryError(f"refusing to drop invalid table name: {table!r}")
        with self._lock:
            self.con.execute(f'DROP TABLE IF EXISTS "{table}"')
            self._table_sources.pop(table, None)

    def load_dataframe(self, table: str, df: pd.DataFrame, source_file: str | None = None) -> int:
        """Atomic load: build a temp table from the dataframe, then swap. A failed load leaves the
        previous table intact (no drop-then-recreate data-loss window)."""
        if not isinstance(table, str) or not re.fullmatch(r"[A-Za-z0-9_]+", table) or table.startswith("_"):
            raise UnsafeQueryError(f"refusing to load invalid table name: {table!r}")
        tmp = f"_load_{table}"
        with self._lock:
            self.con.register("_ingest_df", df)
            try:
                self.con.execute(f'DROP TABLE IF EXISTS "{tmp}"')
                self.con.execute(f'CREATE TABLE "{tmp}" AS SELECT * FROM _ingest_df')
                self.con.execute(f'DROP TABLE IF EXISTS "{table}"')
                self.con.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
                (count,) = self.con.execute(f'SELECT count(*) FROM "{table}"').fetchone()
                if source_file:
                    self._table_sources[table] = source_file
                else:
                    self._table_sources.pop(table, None)
            finally:
                self.con.unregister("_ingest_df")
        return int(count)

    def source_tables(self, sql: str) -> list[tuple[str, str]]:
        """Return actual loaded CSV tables referenced by an already-safe SELECT."""
        tables = self._assert_tables_allowed(self._strip_comments(sql).strip().rstrip(";"))
        with self._lock:
            return [(table, self._table_sources[table]) for table in tables if table in self._table_sources]

    # ---- safe querying -------------------------------------------------
    def run_select(self, sql: str, max_rows: int = 200) -> tuple[list[str], list[tuple]]:
        cleaned = self._strip_comments(sql).strip().rstrip(";")
        if ";" in cleaned:
            raise UnsafeQueryError("multiple statements are not allowed")
        if not re.match(r"^\s*(select|with)\b", cleaned, re.I):
            raise UnsafeQueryError("only SELECT statements are allowed")
        if _RECURSIVE_QUERY.search(cleaned):
            raise UnsafeQueryError("recursive queries are not allowed")
        lowered = cleaned.lower()
        max_rows = max(1, min(int(max_rows), 200))
        for bad in _BLOCKED_TOKENS:
            if bad in lowered:
                raise UnsafeQueryError(f"blocked token in query: {bad!r}")
        # Reject file-reading table functions up front for a clear error (lockdown also blocks them).
        if re.search(r"\bread_\w+\s*\(", lowered) or re.search(r"\b\w+_scan\s*\(", lowered):
            raise UnsafeQueryError("file-reading functions are not allowed")
        if _UNBOUNDED_TABLE_FUNCTIONS.search(cleaned):
            raise UnsafeQueryError("unsupported table function")
        self._assert_tables_allowed(cleaned)

        wrapped = f"SELECT * FROM (\n{cleaned}\n) AS _capped LIMIT {int(max_rows)}"
        with self._lock:
            def execute() -> tuple[list[str], list[tuple]]:
                cur = self.con.execute(wrapped)
                return [d[0] for d in cur.description], cur.fetchmany(int(max_rows))

            columns, rows = self._run_with_timeout(execute)
        return columns, rows

    def _run_with_timeout(self, operation):
        timed_out = threading.Event()

        def interrupt() -> None:
            timed_out.set()
            self.con.interrupt()

        timer = threading.Timer(self._query_timeout_seconds, interrupt)
        timer.daemon = True
        timer.start()
        try:
            return operation()
        except duckdb.Error as exc:
            if timed_out.is_set():
                raise UnsafeQueryError("query execution exceeded the time limit") from exc
            raise
        finally:
            timer.cancel()
            # Join before releasing _lock: a late interrupt must not affect the next query.
            timer.join()

    @staticmethod
    def _strip_comments(sql: str) -> str:
        return _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql))

    def _assert_tables_allowed(self, sql: str) -> list[str]:
        """Parse with DuckDB and require every referenced table to be a known, non-internal table
        (or a CTE defined in the query). Rejects direct file paths, internal/temp tables, unknowns."""
        try:
            with self._lock:
                serialized = self.con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
            ast = json.loads(serialized)
        except Exception as exc:  # unparseable → fail closed
            raise UnsafeQueryError(f"could not parse query: {exc}") from exc

        referenced: list[str] = []
        ctes: set[str] = set()
        table_functions: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                name = node.get("table_name")
                if isinstance(name, str):
                    referenced.append(name)
                if node.get("type") == "TABLE_FUNCTION":
                    function = node.get("function")
                    function_name = function.get("function_name") if isinstance(function, dict) else None
                    if isinstance(function_name, str):
                        table_functions.append(function_name)
                cte_map = node.get("cte_map")
                if isinstance(cte_map, dict):
                    for item in cte_map.get("map", []) or []:
                        key = item.get("key")
                        if isinstance(key, str):
                            ctes.add(key.lower())
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(ast)
        if table_functions:
            raise UnsafeQueryError(f"unsupported table function: {table_functions[0]!r}")
        allowed = {t.lower() for t in self.tables()} | ctes
        for name in referenced:
            if name.lower() in ctes:
                continue
            if name.startswith("_") or name.lower() not in allowed:
                raise UnsafeQueryError(f"query references unknown or internal table: {name!r}")
        return list(dict.fromkeys(name for name in referenced if name.lower() not in ctes))

    def close(self) -> None:
        with self._lock:
            self.con.close()
