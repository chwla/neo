"""Removing a foreign key from an existing SQLite table.

SQLite cannot drop a constraint in place, so the table is rebuilt the way its
documentation prescribes: enforcement off (it cannot change inside a
transaction), a copy under the table's own definition minus the one clause,
the swap, the indexes recreated, and the result checked before the commit. It
is one transaction, so a failure or a crash leaves the table as it was.
"""

from __future__ import annotations

import logging
import re
import sqlite3

logger = logging.getLogger(__name__)


def drop_foreign_key(conn: sqlite3.Connection, table: str, column: str, parent: str) -> bool:
    """Rebuild ``table`` without its ``column -> parent`` foreign key.

    Returns whether the table was rebuilt. A table without that key is left
    alone, so this is safe to run on every start. A rebuild that fails is rolled
    back and logged, and tried again on the next start: it runs while a profile
    opens, which must not fail because of it.
    """

    if not _has_key(conn, table, column, parent):
        return False
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rebuilt = _rebuild(conn, table, column, parent)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except Exception:
        logger.exception("foreign_key_drop_failed table=%s column=%s", table, column)
        return False
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    if rebuilt:
        logger.info("foreign_key_dropped table=%s column=%s parent=%s", table, column, parent)
    return rebuilt


def _has_key(conn: sqlite3.Connection, table: str, column: str, parent: str) -> bool:
    return any(
        row[2] == parent and row[3] == column
        for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')
    )


def _rebuild(conn: sqlite3.Connection, table: str, column: str, parent: str) -> bool:
    # Checked again under the write lock: another connection may have done it.
    if not _has_key(conn, table, column, parent):
        return False
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()[0]
    clause = re.compile(
        rf",\s*FOREIGN\s+KEY\s*\(\s*{re.escape(column)}\s*\)\s*REFERENCES\s+"
        rf"{re.escape(parent)}\s*\(\s*\w+\s*\)",
        re.IGNORECASE,
    )
    without, removed = clause.subn("", create_sql)
    header = re.compile(rf"^CREATE\s+TABLE\s+\"?{re.escape(table)}\"?\s*\(", re.IGNORECASE)
    staging = f"{table}__rebuild"
    staged, renamed = header.subn(f'CREATE TABLE "{staging}" (', without, count=1)
    if removed != 1 or renamed != 1:
        # A definition this code did not write: leave it rather than guess.
        raise RuntimeError(f"unexpected definition for {table}; not rebuilt")

    keys_before = len(conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall())
    violations_before = _violations(conn, table, ignoring=parent)
    indexes = [
        sql
        for (sql,) in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('index', 'trigger') "
            "AND tbl_name = ? AND sql IS NOT NULL",
            (table,),
        )
    ]
    columns = ", ".join(f'"{row[1]}"' for row in conn.execute(f'PRAGMA table_info("{table}")'))
    rows = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]

    conn.execute(staged)
    conn.execute(f'INSERT INTO "{staging}" ({columns}) SELECT {columns} FROM "{table}"')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{staging}" RENAME TO "{table}"')
    for sql in indexes:
        conn.execute(sql)

    if conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] != rows:
        raise RuntimeError(f"{table} lost rows while being rebuilt")
    if len(conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()) != keys_before - 1:
        raise RuntimeError(f"{table} lost a foreign key it should have kept")
    # Rows that already pointed nowhere are not this rebuild's to judge; it may
    # only not add any.
    if _violations(conn, table, ignoring=parent) != violations_before:
        raise RuntimeError(f"{table} breaks a foreign key it kept")
    return True


def _violations(conn: sqlite3.Connection, table: str, *, ignoring: str) -> int:
    return sum(
        1 for row in conn.execute(f'PRAGMA foreign_key_check("{table}")') if row[2] != ignoring
    )
