"""Run arbitrary SQL against the local mirror via DuckDB.

DuckDB's sqlite extension attaches the SQLite file directly, so the full
DuckDB SQL dialect (window functions, ``regexp_matches``, ``list`` aggregates,
``QUALIFY`` ...) is available over the ``playlist_items`` / ``target_order``
tables without copying anything.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from typing import Any

import duckdb

from .db import db_path


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{db_path()}' AS pl (TYPE sqlite, READ_ONLY)")
    con.execute("USE pl")
    return con


def run_query(sql: str, fmt: str = "table") -> None:
    con = _connect()
    try:
        cur = con.execute(sql)
    except duckdb.Error as exc:
        raise SystemExit(f"query error: {exc}") from exc

    columns = [d[0] for d in cur.description or []]
    rows = cur.fetchall()

    if fmt == "json":
        json.dump(
            [dict(zip(columns, r, strict=True)) for r in rows], sys.stdout, indent=2, default=str
        )
        sys.stdout.write("\n")
    elif fmt == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(columns)
        writer.writerows(rows)
    else:
        sys.stdout.write(_ascii_table(columns, rows))
        print(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")


def _ascii_table(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    widths = [len(c) for c in columns]
    str_rows = [["" if v is None else str(v) for v in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    buf = io.StringIO()
    sep = "-+-".join("-" * w for w in widths)
    buf.write(" | ".join(c.ljust(widths[i]) for i, c in enumerate(columns)) + "\n")
    buf.write(sep + "\n")
    for row in str_rows:
        buf.write(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + "\n")
    return buf.getvalue()


def fetch_column(sql: str) -> list[str]:
    """Run ``sql`` and return the first column of every row as strings."""
    con = _connect()
    try:
        rows = con.execute(sql).fetchall()
    except duckdb.Error as exc:
        raise SystemExit(f"query error: {exc}") from exc
    return [str(r[0]) for r in rows]
