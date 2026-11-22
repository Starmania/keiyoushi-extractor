import sqlite3
from importlib.resources import files
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(files("keiex").joinpath("schema.sql").read_text())
    migrate(conn)
    return conn


# Columns added after a database was first published: (table, column, type).
ADDED_COLUMNS = [
    ("extension", "theme_path", "TEXT"),
    ("extension", "source_url", "TEXT"),
    ("source", "stable_id", "TEXT"),
    ("status_snapshot", "error", "TEXT"),
    ("status_snapshot", "notes", "TEXT"),
    ("status_snapshot", "rechecks", "TEXT"),
]


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in ADDED_COLUMNS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()


def get_kv(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
