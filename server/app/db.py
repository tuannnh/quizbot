"""SQLite storage for captures and answers."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS answers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    session       TEXT NOT NULL,
    machine       TEXT NOT NULL,
    source        TEXT NOT NULL,
    status        TEXT NOT NULL,          -- pending | done | no_question | error
    question_type TEXT,
    question      TEXT,
    options       TEXT,                   -- JSON list
    answer_keys   TEXT,                   -- JSON list
    answer        TEXT,
    shape         TEXT,
    color         TEXT,
    confidence    REAL,
    reasoning     TEXT,
    model         TEXT,
    latency_ms    INTEGER,
    image_path    TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_answers_session ON answers(session, id);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after v0.1; created on startup if an older database is opened.
MIGRATIONS = {
    "timings": "TEXT",          # JSON: {capture_ms, network_ms, decode_ms, prepare_ms, model_ms, server_ms, roundtrip_ms}
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "image_px": "TEXT",         # e.g. "1568x1045" as sent to the model
}

JSON_COLS = ("options", "answer_keys", "timings")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(answers)")}
            for col, typ in MIGRATIONS.items():
                if col not in existing:
                    self._conn.execute(f"ALTER TABLE answers ADD COLUMN {col} {typ}")
            self._conn.commit()

    def create_pending(self, session: str, machine: str, source: str) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO answers (created_at, session, machine, source, status) VALUES (?,?,?,?, 'pending')",
                (now, session, machine, source),
            )
            return int(cur.lastrowid)

    def update(self, row_id: int, **fields) -> None:
        for col in JSON_COLS:
            if col in fields and not isinstance(fields[col], str):
                fields[col] = json.dumps(fields[col], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE answers SET {cols} WHERE id = ?", (*fields.values(), row_id))

    def get(self, row_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM answers WHERE id = ?", (row_id,)).fetchone()
        return _to_dict(row) if row else None

    def image_path(self, row_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT image_path FROM answers WHERE id = ?", (row_id,)).fetchone()
        return row["image_path"] if row else None

    def list(self, session: str | None = None, machine: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM answers WHERE 1=1", []
        if session:
            sql += " AND session = ?"
            args.append(session)
        if machine:
            sql += " AND machine = ?"
            args.append(machine)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_to_dict(r) for r in rows]

    def merge_timings(self, row_id: int, extra: dict) -> dict | None:
        """Add client-side timings (reported after the round trip) to a record."""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT timings FROM answers WHERE id = ?", (row_id,)).fetchone()
            if not row:
                return None
            timings = json.loads(row["timings"]) if row["timings"] else {}
            timings.update(extra)
            self._conn.execute("UPDATE answers SET timings = ? WHERE id = ?", (json.dumps(timings), row_id))
        return self.get(row_id)

    def timing_rows(self, session: str | None = None) -> list[dict]:
        sql = "SELECT model, timings, input_tokens FROM answers WHERE status IN ('done','no_question') AND timings IS NOT NULL"
        args: list = []
        if session:
            sql += " AND session = ?"
            args.append(session)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [{"model": r["model"], "timings": json.loads(r["timings"]), "input_tokens": r["input_tokens"]} for r in rows]

    def captures(self, limit: int) -> list[tuple[int, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, image_path FROM answers WHERE image_path IS NOT NULL AND status = 'done' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(r["id"], r["image_path"]) for r in rows]

    def get_settings(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def put_settings(self, values: dict[str, str | None]) -> dict[str, str]:
        """Set keys; an empty or None value removes the key (falls back to the default)."""
        with self._lock, self._conn:
            for key, value in values.items():
                if value:
                    self._conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
                else:
                    self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return self.get_settings()

    def delete_answers(self, session: str | None = None) -> list[str]:
        """Delete answers (all, or one session). Returns image paths so the caller can remove files."""
        where, args = ("WHERE session = ?", [session]) if session else ("", [])
        with self._lock, self._conn:
            paths = [r["image_path"] for r in self._conn.execute(
                f"SELECT image_path FROM answers {where}", args) if r["image_path"]]
            self._conn.execute(f"DELETE FROM answers {where}", args)
        return paths

    def facets(self) -> dict:
        with self._lock:
            sessions = self._conn.execute(
                "SELECT session, COUNT(*) n, MAX(created_at) last FROM answers GROUP BY session ORDER BY last DESC"
            ).fetchall()
            machines = self._conn.execute(
                "SELECT machine, COUNT(*) n, MAX(created_at) last FROM answers GROUP BY machine ORDER BY last DESC"
            ).fetchall()
        return {
            "sessions": [dict(r) for r in sessions],
            "machines": [dict(r) for r in machines],
        }


def _to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for col in JSON_COLS:
        d[col] = json.loads(d[col]) if d.get(col) else ({} if col == "timings" else [])
    d["has_image"] = bool(d.pop("image_path", None))
    return d
