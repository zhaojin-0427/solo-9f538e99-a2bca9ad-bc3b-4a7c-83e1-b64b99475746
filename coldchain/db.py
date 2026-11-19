"""SQLite schema and connection handling.

Concurrency model
-----------------
* Every mutating operation opens a ``BEGIN IMMEDIATE`` transaction, so only
  one writer can be inside the write path at a time. Readers use short
  transactions and see a consistent snapshot.
* Month facts, knowledge versions, version ranges and audit rows are
  append-only. There are triggers preventing UPDATE/DELETE on them.
* A knowledge version is inserted in the same transaction as its ranges;
  the transaction either commits the whole group or rolls it back.

Multi-warehouse model (schema v2)
---------------------------------
* Each warehouse freezes its own UTC month closes. The natural key of
  ``months`` is ``(warehouse, month)`` and shipment sequence numbers are
  unique *per warehouse* (``UNIQUE(warehouse, sequence_no)``). Two
  warehouses may therefore both book sequence 001 for the same month,
  while one warehouse can never reuse a sequence across months.
* Databases created by the v1 schema (no ``warehouse`` column) are
  upgraded in place inside a single ``BEGIN IMMEDIATE`` transaction: every
  legacy month/shipment row is copied into the default warehouse together
  with its source filename and source row number. If the upgrade is
  interrupted the transaction rolls back and the original volume retries
  the same migration on the next start, so old records are neither lost
  nor duplicated.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_STATEMENTS = [
    "PRAGMA foreign_keys = ON",
    """
CREATE TABLE IF NOT EXISTS warehouses (
    warehouse            TEXT PRIMARY KEY,
    created_at           TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS months (
    warehouse            TEXT NOT NULL REFERENCES warehouses(warehouse),
    month                TEXT NOT NULL,
    source_filename      TEXT NOT NULL,
    source_sha256        TEXT NOT NULL,
    row_count            INTEGER NOT NULL,
    total_boxes          INTEGER NOT NULL,
    frozen_at            TEXT NOT NULL,
    PRIMARY KEY (warehouse, month)
)
""",
    """
CREATE TABLE IF NOT EXISTS shipments (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse            TEXT NOT NULL,
    month                TEXT NOT NULL,
    sequence_no          TEXT NOT NULL,
    sku                  TEXT NOT NULL,
    occurred_at          TEXT NOT NULL,
    occurred_at_us       INTEGER NOT NULL,
    boxes                INTEGER NOT NULL CHECK (boxes > 0),
    source_row           INTEGER NOT NULL,
    UNIQUE (warehouse, sequence_no),
    FOREIGN KEY (warehouse, month) REFERENCES months(warehouse, month)
)
""",
    "CREATE INDEX IF NOT EXISTS idx_shipments_wh_month ON shipments(warehouse, month)",
    "CREATE INDEX IF NOT EXISTS idx_shipments_wh_month_sku ON shipments(warehouse, month, sku)",
    """
CREATE TABLE IF NOT EXISTS knowledge_versions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no           INTEGER NOT NULL UNIQUE,
    based_on_version     INTEGER REFERENCES knowledge_versions(version_no),
    reason               TEXT NOT NULL,
    created_at           TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS version_ranges (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no           INTEGER NOT NULL REFERENCES knowledge_versions(version_no),
    sku                  TEXT NOT NULL,
    zone                 TEXT NOT NULL,
    valid_from           TEXT NOT NULL,
    valid_from_us        INTEGER NOT NULL,
    valid_to             TEXT,
    valid_to_us          INTEGER,
    UNIQUE (version_no, sku, valid_from_us),
    CHECK (valid_to_us IS NULL OR valid_to_us > valid_from_us)
)
""",
    "CREATE INDEX IF NOT EXISTS idx_ranges_version_sku"
    " ON version_ranges(version_no, sku, valid_from_us, valid_to_us)",
    """
CREATE TABLE IF NOT EXISTS audit_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    at                   TEXT NOT NULL,
    action               TEXT NOT NULL,
    month                TEXT,
    version_no           INTEGER,
    summary              TEXT NOT NULL
)
""",
]

SCHEMA = ";\n".join(SCHEMA_STATEMENTS) + ";\n"

# Append-only protections. Facts and the knowledge ledger are never rewritten;
# corrections are new versions, not edits.
TRIGGER_STATEMENTS = [
    """
CREATE TRIGGER IF NOT EXISTS warehouses_no_update
BEFORE UPDATE ON warehouses BEGIN
    SELECT RAISE(ABORT, 'warehouse registry is immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS warehouses_no_delete
BEFORE DELETE ON warehouses BEGIN
    SELECT RAISE(ABORT, 'warehouse registry is immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS months_no_update
BEFORE UPDATE ON months BEGIN
    SELECT RAISE(ABORT, 'month facts are immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS months_no_delete
BEFORE DELETE ON months BEGIN
    SELECT RAISE(ABORT, 'month facts are immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS shipments_no_update
BEFORE UPDATE ON shipments BEGIN
    SELECT RAISE(ABORT, 'shipment facts are immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS shipments_no_delete
BEFORE DELETE ON shipments BEGIN
    SELECT RAISE(ABORT, 'shipment facts are immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS versions_no_update
BEFORE UPDATE ON knowledge_versions BEGIN
    SELECT RAISE(ABORT, 'knowledge versions are immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS versions_no_delete
BEFORE DELETE ON knowledge_versions BEGIN
    SELECT RAISE(ABORT, 'knowledge versions are immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS ranges_no_update
BEFORE UPDATE ON version_ranges BEGIN
    SELECT RAISE(ABORT, 'knowledge versions are immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS ranges_no_delete
BEFORE DELETE ON version_ranges BEGIN
    SELECT RAISE(ABORT, 'knowledge versions are immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS ranges_no_overlap
BEFORE INSERT ON version_ranges
WHEN EXISTS (
    SELECT 1 FROM version_ranges r
    WHERE r.version_no = NEW.version_no
      AND r.sku = NEW.sku
      AND (NEW.valid_to_us IS NULL OR r.valid_from_us < NEW.valid_to_us)
      AND (r.valid_to_us IS NULL OR r.valid_to_us > NEW.valid_from_us)
)
BEGIN
    SELECT RAISE(ABORT, 'overlapping ranges for the same sku in one version');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END
""",
]

IMMUTABLE_TRIGGERS = ";\n".join(TRIGGER_STATEMENTS) + ";\n"

# DDL for the v2 fact tables, reused by the legacy migration (which cannot
# use executescript because that would commit the migration transaction).
FACT_DDL_STATEMENTS = [
    SCHEMA_STATEMENTS[1],  # warehouses
    SCHEMA_STATEMENTS[2],  # months
    SCHEMA_STATEMENTS[3],  # shipments
    SCHEMA_STATEMENTS[4],  # idx_shipments_wh_month
    SCHEMA_STATEMENTS[5],  # idx_shipments_wh_month_sku
]

SCHEMA_VERSION = 2


def db_path_from_env(default=None):
    configured = os.environ.get("COLDCHAIN_DB")
    if configured:
        return Path(configured)
    if default:
        return Path(default)
    return Path(__file__).resolve().parent.parent / "data" / "coldchain.sqlite3"


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _is_legacy_database(conn):
    """A v1 database has months/shipments tables without a warehouse column."""
    if not _table_exists(conn, "months"):
        return False
    return "warehouse" not in _table_columns(conn, "months")


def _migrate_legacy_database(conn):
    """Upgrade a v1 volume in one atomic transaction.

    All DDL and DML here run inside one ``BEGIN IMMEDIATE`` transaction.
    SQLite transactions cover schema changes too, so any failure (including
    a crash mid-upgrade) rolls the rename/copy back: the legacy tables stay
    in place and the next startup retries the migration from scratch.
    """
    from .timeutil import now_str
    from .warehouses import DEFAULT_WAREHOUSE

    # Foreign keys must be off while tables are rebuilt; re-enabled afterwards.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE months RENAME TO months_legacy")
            conn.execute("ALTER TABLE shipments RENAME TO shipments_legacy")

            for statement in FACT_DDL_STATEMENTS:
                conn.execute(statement)

            frozen_at = now_str()
            conn.execute(
                "INSERT INTO warehouses(warehouse, created_at) VALUES (?, ?)",
                (DEFAULT_WAREHOUSE, frozen_at),
            )
            # Every legacy fact belongs to the default warehouse, keeping
            # its original source filename and source row number.
            conn.execute(
                "INSERT INTO months(warehouse, month, source_filename, source_sha256,"
                " row_count, total_boxes, frozen_at)"
                " SELECT ?, month, source_filename, source_sha256, row_count,"
                " total_boxes, frozen_at FROM months_legacy",
                (DEFAULT_WAREHOUSE,),
            )
            conn.execute(
                "INSERT INTO shipments(id, warehouse, month, sequence_no, sku,"
                " occurred_at, occurred_at_us, boxes, source_row)"
                " SELECT id, ?, month, sequence_no, sku, occurred_at,"
                " occurred_at_us, boxes, source_row FROM shipments_legacy",
                (DEFAULT_WAREHOUSE,),
            )
            for statement in TRIGGER_STATEMENTS:
                conn.execute(statement)

            conn.execute("DROP TABLE shipments_legacy")
            conn.execute("DROP TABLE months_legacy")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_db(path):
    conn = connect(path)
    try:
        if _is_legacy_database(conn):
            _migrate_legacy_database(conn)
            return
        conn.executescript(SCHEMA)
        conn.executescript(IMMUTABLE_TRIGGERS)
        # Fresh and already-upgraded databases both make sure the default
        # warehouse exists; old links/APIs without a warehouse code use it.
        from .timeutil import now_str
        from .warehouses import DEFAULT_WAREHOUSE

        conn.execute(
            "INSERT OR IGNORE INTO warehouses(warehouse, created_at) VALUES (?, ?)",
            (DEFAULT_WAREHOUSE, now_str()),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


class transaction:
    """``BEGIN IMMEDIATE`` context manager that never leaves a half write.

    Any exception rolls the whole group back. ``commit`` happens exactly once
    when the block finishes without error.
    """

    def __init__(self, conn, immediate=True):
        self.conn = conn
        self.immediate = immediate
        self.depth = 0

    def __enter__(self):
        if self.depth == 0:
            self.conn.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
        self.depth += 1
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.depth -= 1
        if self.depth > 0:
            return False
        if exc_type is not None:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            return False
        try:
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return False
