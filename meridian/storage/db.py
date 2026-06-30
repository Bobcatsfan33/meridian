"""DuckDB connection + schema application (idempotent)."""
from __future__ import annotations
import contextlib
import csv
import pathlib
import duckdb

SCHEMA = pathlib.Path(__file__).with_name("schema.sql")


def connect(db_path: pathlib.Path) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


@contextlib.contextmanager
def db(target):
    """Context manager that ALWAYS closes the connection (fd-leak safe).

    `with db(cfg) as con:` or `with db(path) as con:`. Accepts a Config (uses its
    duckdb_path) or a path. Use this for any short-lived connection.
    """
    path = getattr(target, "duckdb_path", target)
    con = connect(path)
    try:
        yield con
    finally:
        con.close()


def apply_schema(con: duckdb.DuckDBPyConnection) -> int:
    sql = SCHEMA.read_text()
    con.execute(sql)
    return sql.count("CREATE TABLE")


def load_universe(con: duckdb.DuckDBPyConnection, universe_csv: pathlib.Path) -> int:
    con.execute("CREATE OR REPLACE TABLE universe (symbol VARCHAR, name VARCHAR, "
                "sector VARCHAR, index_membership VARCHAR)")
    rows = list(csv.DictReader(universe_csv.open()))
    con.executemany(
        "INSERT INTO universe VALUES (?,?,?,?)",
        [(r["symbol"], r["name"], r["sector"], r["index_membership"]) for r in rows],
    )
    return len(rows)


def init_db(db_path: pathlib.Path, universe_csv: pathlib.Path) -> dict:
    con = connect(db_path)
    n_tables = apply_schema(con)
    n_universe = load_universe(con, universe_csv)
    con.execute("INSERT OR REPLACE INTO schema_meta VALUES ('initialized','true')")
    con.close()
    return {"tables_declared": n_tables, "universe_rows": n_universe}


def table_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    names = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' ORDER BY table_name").fetchall()]
    out = {}
    for n in names:
        out[n] = con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]
    return out
