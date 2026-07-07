"""DuckDB demo warehouse: creation, health checks, and guarded SQL access."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

READ_ONLY_PREFIXES = {"SELECT", "WITH", "DESCRIBE", "SHOW", "EXPLAIN"}
MAX_ROWS = 50


@dataclass
class ModelDef:
    """A SQL model in the demo pipeline (mirrors a dbt-style project)."""

    name: str
    depends_on: list[str]
    sql_file: str
    description: str
    owner: str
    materialized: bool = True  # False = metadata-only asset (e.g. ML scoring output)


def read_model_sql(models_dir: Path, model_name: str) -> str:
    path = models_dir / f"{model_name}.sql"
    return path.read_text(encoding="utf-8")


def create_raw_tables(con: duckdb.DuckDBPyConnection) -> None:
    rng = random.Random(42)
    con.execute("DROP TABLE IF EXISTS raw_payments CASCADE")
    con.execute("DROP TABLE IF EXISTS raw_customers CASCADE")
    con.execute(
        """
        CREATE TABLE raw_customers (
            customer_id INTEGER,
            name VARCHAR,
            country VARCHAR,
            signup_date DATE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE raw_payments (
            payment_id INTEGER,
            customer_id INTEGER,
            amount_usd DOUBLE,
            currency VARCHAR,
            created_at TIMESTAMP
        )
        """
    )
    names = ["Acme Corp", "Globex", "Initech", "Umbrella", "Stark Ind", "Wayne Ent", "Hooli", "Pied Piper"]
    countries = ["US", "DE", "GB", "US", "JP", "US", "DE", "GB"]
    for i, (name, country) in enumerate(zip(names, countries), start=1):
        con.execute(
            "INSERT INTO raw_customers VALUES (?, ?, ?, ?)",
            [i, name, country, date(2025, 1, 1) + timedelta(days=rng.randint(0, 200))],
        )
    base = datetime(2026, 6, 20, 8, 0, 0)
    for pid in range(1, 61):
        con.execute(
            "INSERT INTO raw_payments VALUES (?, ?, ?, ?, ?)",
            [
                pid,
                rng.randint(1, 8),
                round(rng.uniform(20, 900), 2),
                rng.choice(["USD", "USD", "USD", "EUR"]),
                base + timedelta(hours=rng.randint(0, 24 * 14)),
            ],
        )


def create_views(con: duckdb.DuckDBPyConnection, models: list[ModelDef], models_dir: Path) -> None:
    for model in models:
        if not model.materialized:
            continue
        sql = read_model_sql(models_dir, model.name)
        con.execute(f"CREATE OR REPLACE VIEW {model.name} AS {sql}")


def apply_upstream_break(con: duckdb.DuckDBPyConnection) -> None:
    """Simulate the vendor feed v2 change: `amount_usd` arrives renamed to `amount`."""
    con.execute("ALTER TABLE raw_payments RENAME COLUMN amount_usd TO amount")


def describe_table(warehouse_path: Path, table: str) -> list[tuple[str, str]]:
    """Return [(column_name, duckdb_type)] for a table or view."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        rows = con.execute(f"DESCRIBE {table}").fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        con.close()


def health_check(warehouse_path: Path, models: list[ModelDef]) -> list[dict]:
    """Try to query every materialized model; return per-model status."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    results = []
    try:
        for model in models:
            if not model.materialized:
                continue
            try:
                con.execute(f"SELECT * FROM {model.name} LIMIT 5").fetchall()
                results.append({"model": model.name, "ok": True, "error": None})
            except Exception as exc:  # noqa: BLE001 - the error text is the signal
                results.append({"model": model.name, "ok": False, "error": str(exc)})
    finally:
        con.close()
    return results


def run_readonly_sql(warehouse_path: Path, sql: str) -> str:
    """Execute a read-only query and format the result (or the error) as text."""
    first_word = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    if first_word not in READ_ONLY_PREFIXES:
        return f"ERROR: only read-only queries are allowed ({', '.join(sorted(READ_ONLY_PREFIXES))})."
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        cursor = con.execute(sql)
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(MAX_ROWS)
        lines = [" | ".join(columns)]
        lines += [" | ".join(str(v) for v in row) for row in rows]
        if len(rows) == MAX_ROWS:
            lines.append(f"... (truncated at {MAX_ROWS} rows)")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return f"QUERY FAILED: {exc}"
    finally:
        con.close()


def recreate_view(warehouse_path: Path, model_name: str, sql: str) -> None:
    con = duckdb.connect(str(warehouse_path))
    try:
        con.execute(f"CREATE OR REPLACE VIEW {model_name} AS {sql}")
    finally:
        con.close()
