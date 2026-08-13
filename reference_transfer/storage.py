from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def now() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime | None = None) -> str:
    return (value or now()).isoformat()


def parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Job:
    task_id: str
    payload: dict[str, Any]
    content_hash: str
    state: str
    attempt_count: int


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              task_id TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              state TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT NOT NULL,
              lease_until TEXT,
              last_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mappings (
              todoist_task_id TEXT PRIMARY KEY,
              workflowy_node_id TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rate_limits (
              platform TEXT PRIMARY KEY,
              tokens REAL NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, next_attempt_at, created_at);
            """
        )
        self.connection.commit()

    def enqueue(self, task_id: str, payload: dict[str, Any], content_hash: str) -> str:
        current = stamp()
        with self.connection:
            existing = self.connection.execute("SELECT state FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)",
                    (task_id, json.dumps(payload, sort_keys=True), content_hash, current, current, current),
                )
                return "created"
            if existing["state"] in {"completed", "cancelled", "blocked"}:
                return "unchanged"
            self.connection.execute(
                """UPDATE jobs SET payload_json=?, content_hash=?, state='pending', next_attempt_at=?,
                   lease_until=NULL, last_error=NULL, updated_at=? WHERE task_id=?""",
                (json.dumps(payload, sort_keys=True), content_hash, current, current, task_id),
            )
            return "updated"

    def reclaim_expired_leases(self) -> int:
        with self.connection:
            result = self.connection.execute(
                "UPDATE jobs SET state='retry', lease_until=NULL, next_attempt_at=?, updated_at=? "
                "WHERE state='processing' AND lease_until < ?",
                (stamp(), stamp(), stamp()),
            )
        return result.rowcount

    def release_processing(self) -> int:
        """Return jobs claimed by a deliberately stopped worker to the queue."""
        with self.connection:
            result = self.connection.execute(
                "UPDATE jobs SET state='retry', lease_until=NULL, next_attempt_at=?, updated_at=? "
                "WHERE state='processing'",
                (stamp(), stamp()),
            )
        return result.rowcount

    def claim(self, limit: int, lease_seconds: int) -> list[Job]:
        current = now()
        lease = stamp(current + timedelta(seconds=lease_seconds))
        with self.connection:
            rows = self.connection.execute(
                """SELECT task_id FROM jobs WHERE state IN ('pending', 'retry') AND next_attempt_at <= ?
                   ORDER BY created_at, task_id LIMIT ?""",
                (stamp(current), limit),
            ).fetchall()
            ids = [row["task_id"] for row in rows]
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            self.connection.execute(
                f"UPDATE jobs SET state='processing', lease_until=?, updated_at=? WHERE task_id IN ({marks})",
                (lease, stamp(current), *ids),
            )
            claimed = self.connection.execute(
                f"SELECT * FROM jobs WHERE task_id IN ({marks}) ORDER BY created_at, task_id", ids
            ).fetchall()
        return [self._job(row) for row in claimed]

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(row["task_id"], json.loads(row["payload_json"]), row["content_hash"], row["state"], row["attempt_count"])

    def set_job(self, task_id: str, state: str, *, next_attempt_at: datetime | None = None,
                error: str | None = None, increment_attempt: bool = False) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE jobs SET state=?, next_attempt_at=?, lease_until=NULL, last_error=?,
                   attempt_count=attempt_count + ?, updated_at=? WHERE task_id=?""",
                (state, stamp(next_attempt_at), error, int(increment_attempt), stamp(), task_id),
            )

    def get_mapping(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM mappings WHERE todoist_task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def save_mapping(self, task_id: str, node_id: str, content_hash: str, status: str) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO mappings(todoist_task_id, workflowy_node_id, content_hash, status, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(todoist_task_id) DO UPDATE SET workflowy_node_id=excluded.workflowy_node_id,
                   content_hash=excluded.content_hash, status=excluded.status, updated_at=excluded.updated_at""",
                (task_id, node_id, content_hash, status, stamp()),
            )

    def set_mapping_status(self, task_id: str, status: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE mappings SET status=?, updated_at=? WHERE todoist_task_id=?", (status, stamp(), task_id))

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
            )

    def dependency_open(self, task_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM jobs WHERE task_id=? AND state IN ('pending', 'retry', 'processing', 'retained')", (task_id,)
        ).fetchone()
        return row is not None

    def consume_token(self, platform: str, capacity_per_minute: int) -> float:
        """Consume one shared token and return a necessary wait duration in seconds."""
        current = now()
        with self.connection:
            row = self.connection.execute("SELECT * FROM rate_limits WHERE platform=?", (platform,)).fetchone()
            if row is None:
                tokens, updated = float(capacity_per_minute), current
            else:
                updated = parse_stamp(row["updated_at"])
                tokens = min(float(capacity_per_minute), row["tokens"] + (current - updated).total_seconds() * capacity_per_minute / 60)
            if tokens >= 1:
                remaining, wait = tokens - 1, 0.0
            else:
                remaining, wait = tokens, (1 - tokens) * 60 / capacity_per_minute
            self.connection.execute(
                """INSERT INTO rate_limits VALUES (?, ?, ?) ON CONFLICT(platform) DO UPDATE SET
                   tokens=excluded.tokens, updated_at=excluded.updated_at""",
                (platform, remaining, stamp(current)),
            )
        return wait
