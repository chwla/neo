from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from app.services.bundles.redaction import redact

JSON_COLUMNS = ("_json",)


def rows(
    conn: sqlite3.Connection, table: str, where: str = "1=1", params: Iterable = ()
) -> list[dict]:
    try:
        result = [
            dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE {where}", tuple(params))
        ]
    except sqlite3.OperationalError:
        return []
    for item in result:
        for key, value in list(item.items()):
            if key.endswith(JSON_COLUMNS) and value:
                try:
                    item[key.removesuffix("_json")] = json.loads(value)
                except json.JSONDecodeError:
                    pass
            item.pop(key, None) if key.endswith(JSON_COLUMNS) else None
    return redact(result)
