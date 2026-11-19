"""Warehouse code validation and resolution.

Warehouse codes are short, human-chosen identifiers. All legacy rows and all
requests that do not state a warehouse belong to :data:`DEFAULT_WAREHOUSE`,
so old links and API calls keep working unchanged.
"""

from __future__ import annotations

import re

from .errors import ValidationError

DEFAULT_WAREHOUSE = "default"
MAX_WAREHOUSE_LENGTH = 64

# Letters, digits, dash and underscore only; must start with a letter/digit.
_WAREHOUSE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def normalize_warehouse(value):
    """Return a validated warehouse code.

    Empty/``None`` resolves to the default warehouse so legacy clients and
    bookmarked links without a warehouse parameter keep reading and writing
    the same data as before the upgrade.
    """
    if value is None:
        return DEFAULT_WAREHOUSE
    text = str(value).strip()
    if not text:
        return DEFAULT_WAREHOUSE
    if not _WAREHOUSE_RE.match(text) or len(text) > MAX_WAREHOUSE_LENGTH:
        raise ValidationError(
            "仓号只能包含字母、数字、连字符或下划线，且以字母或数字开头"
            f"（最长 {MAX_WAREHOUSE_LENGTH} 字符）"
        )
    return text


def list_warehouses(conn):
    return conn.execute(
        "SELECT w.warehouse, w.created_at,"
        " (SELECT COUNT(*) FROM months m WHERE m.warehouse = w.warehouse) AS month_count"
        " FROM warehouses w ORDER BY"
        " CASE WHEN w.warehouse = ? THEN 0 ELSE 1 END, w.warehouse",
        (DEFAULT_WAREHOUSE,),
    ).fetchall()


def ensure_warehouse(conn, warehouse):
    """Register a warehouse inside the current write transaction.

    Warehouse codes are created lazily on first use; the registry itself is
    immutable afterwards (no update/delete triggers).
    """
    from .timeutil import now_str

    conn.execute(
        "INSERT OR IGNORE INTO warehouses(warehouse, created_at) VALUES (?, ?)",
        (warehouse, now_str()),
    )
