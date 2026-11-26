"""Database access layer.

SQLite for local development (file path from DB_PATH, default ./smartmatch.db).
PostgreSQL when DATABASE_URL is set (e.g. Neon free tier in production).
app.py speaks one small API: connect(), Row (dict + positional access),
translate of '?' placeholders, and backend-aware helpers.
"""
import os
import sqlite3
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL")
PG = bool(DATABASE_URL)

if PG:
    import psycopg  # psycopg 3
    IntegrityError = psycopg.IntegrityError
else:
    IntegrityError = sqlite3.IntegrityError

# Stable key for the Postgres advisory lock taken during generation.
ADVISORY_LOCK_KEY = 88172645


def database_path():
    configured = os.environ.get("DB_PATH")
    if configured:
        return configured
    if os.environ.get("VERCEL") or os.environ.get("RENDER"):
        return "/tmp/smartmatch.db"
    return "smartmatch.db"


class Row(dict):
    """dict row that also supports row[0] positional access (like sqlite3.Row)."""

    __slots__ = ("_vals",)

    def __init__(self, cols, vals):
        super().__init__(zip(cols, vals))
        self._vals = list(vals)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, raw, cols):
        self._raw = raw
        self._cols = cols

    @property
    def rowcount(self):
        return self._raw.rowcount

    def fetchone(self):
        r = self._raw.fetchone()
        return Row(self._cols, r) if r is not None else None

    def fetchall(self):
        return [Row(self._cols, r) for r in self._raw.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        if PG:
            sql = sql.replace("?", "%s")
        cur = self._raw.execute(sql, params)
        cols = [d[0] for d in (cur.description or [])]
        return Cursor(cur, cols)

    def executescript(self, script):
        if PG:
            for stmt in script.split(";"):
                if stmt.strip():
                    self._raw.execute(stmt)
        else:
            self._raw.executescript(script)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def connect():
    if PG:
        return Connection(psycopg.connect(DATABASE_URL))
    raw = sqlite3.connect(database_path(), timeout=30)
    raw.execute("PRAGMA foreign_keys=ON")
    return Connection(raw)


@contextmanager
def savepoint(c):
    """Wrap one statement so a constraint violation doesn't abort a Postgres transaction."""
    if not PG:
        yield
        return
    c.execute("SAVEPOINT sp_one")
    try:
        yield
        c.execute("RELEASE SAVEPOINT sp_one")
    except BaseException:
        c.execute("ROLLBACK TO SAVEPOINT sp_one")
        raise


def begin_immediate(c):
    """Serialise concurrent generation on both backends."""
    if PG:
        c.execute("SELECT pg_advisory_xact_lock(?)", (ADVISORY_LOCK_KEY,))
    else:
        c.execute("BEGIN IMMEDIATE")


def insert_ignore(c, sql, params=()):
    """SQLite 'INSERT OR IGNORE INTO ...' portable to Postgres."""
    if PG:
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO") + " ON CONFLICT DO NOTHING"
    return c.execute(sql, params)


def table_columns(c, table):
    if PG:
        return [r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,))]
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def reset_identity_sequences(c):
    """After a snapshot restore inserts explicit ids, realign Postgres identity sequences."""
    if not PG:
        return
    for t in ("matches", "transfers", "audit", "snapshots"):
        c.execute(
            f"SELECT setval(pg_get_serial_sequence('{t}','id'), "
            f"COALESCE((SELECT MAX(id) FROM {t}), 1))")
