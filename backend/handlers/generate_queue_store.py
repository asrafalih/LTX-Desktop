"""SQLite durability for projectName video generate jobs."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from api_types import GenerateVideoRequest

logger = logging.getLogger(__name__)

GenerateJobStatus = Literal["queued", "running", "complete", "failed", "cancelled"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generate_jobs (
    id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    project_name TEXT NOT NULL,
    video_path TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generate_jobs_incomplete
    ON generate_jobs (status, created_at, id);
"""


@dataclass(frozen=True)
class GenerateJobRecord:
    id: str
    request: GenerateVideoRequest
    status: GenerateJobStatus
    project_name: str
    video_path: str
    error: str | None
    created_at: float
    updated_at: float


class GenerateQueueStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn
        except sqlite3.Error:
            logger.exception("Corrupt generate queue DB at %s — recreating", self._path)
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn

    def insert_queued(self, job_id: str, req: GenerateVideoRequest) -> None:
        now = time.time()
        project_name = (req.projectName or "").strip()
        self._conn.execute(
            """
            INSERT INTO generate_jobs
                (id, request_json, status, project_name, video_path, error, created_at, updated_at)
            VALUES (?, ?, 'queued', ?, '', NULL, ?, ?)
            """,
            (job_id, req.model_dump_json(), project_name, now, now),
        )
        self._conn.commit()

    def set_status(
        self,
        job_id: str,
        status: GenerateJobStatus,
        *,
        video_path: str | None = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        if video_path is not None:
            self._conn.execute(
                """
                UPDATE generate_jobs
                SET status = ?, video_path = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, video_path, error, now, job_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE generate_jobs
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, now, job_id),
            )
        self._conn.commit()

    def get(self, job_id: str) -> GenerateJobRecord | None:
        row = self._conn.execute(
            "SELECT * FROM generate_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return None if row is None else self._row_to_record(row)

    def list_incomplete(self) -> list[GenerateJobRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM generate_jobs
            WHERE status IN ('queued', 'running')
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def incomplete_ids(self) -> set[str]:
        return {job.id for job in self.list_incomplete()}

    def reset_running_to_queued(self) -> None:
        now = time.time()
        self._conn.execute(
            """
            UPDATE generate_jobs
            SET status = 'queued', updated_at = ?
            WHERE status = 'running'
            """,
            (now,),
        )
        self._conn.commit()

    def _row_to_record(self, row: sqlite3.Row) -> GenerateJobRecord:
        return GenerateJobRecord(
            id=row["id"],
            request=GenerateVideoRequest.model_validate_json(row["request_json"]),
            status=row["status"],
            project_name=row["project_name"],
            video_path=row["video_path"] or "",
            error=row["error"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
