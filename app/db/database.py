import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.core.config import get_settings


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations" / "sqlite"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(get_settings().sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Apply versioned SQL migrations (migrations/sqlite/NNN_*.sql) exactly once each."""
    with connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (path.stem, now()))


def insert(table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    serialized = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in values.values()]
    with connection() as conn:
        conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", serialized)


def update(table: str, key: str, key_value: str, values: dict[str, Any]) -> None:
    assignments = ", ".join(f"{column} = ?" for column in values)
    serialized = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in values.values()]
    with connection() as conn:
        conn.execute(f"UPDATE {table} SET {assignments} WHERE {key} = ?", [*serialized, key_value])


def get_one(table: str, key: str, value: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (value,)).fetchone()
    return decode_row(dict(row)) if row else None


def list_rows(table: str, where: str = "", args: tuple[Any, ...] = (), limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(f"SELECT * FROM {table} {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    return [decode_row(dict(row)) for row in rows]


def decode_row(row: dict[str, Any]) -> dict[str, Any]:
    json_columns = {"geometry", "source_scene_ids", "quality", "provenance", "evidence", "dates", "candidate_ids", "rationale", "result", "payload", "provider_response", "assumptions", "location", "coverage", "contacts", "bbox", "properties", "training", "metrics", "protocol", "cells", "params", "details", "evidence_mix", "detection", "verification", "forecast", "impact", "station", "route", "notify", "ivr", "field_updates", "history"}
    for key, value in list(row.items()):
        if key in json_columns and isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return row


def audit(actor_role: str, action: str, resource_type: str, resource_id: str | None, details: dict[str, Any]) -> None:
    insert("audit_events", {"actor_role": actor_role, "action": action, "resource_type": resource_type, "resource_id": resource_id, "details": details, "created_at": now()})
