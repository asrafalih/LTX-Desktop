"""Persist pending project-ingest jobs for the Electron gallery watcher."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from api_types import GenerateVideoRequest, ProjectIngestJob, ProjectIngestListResponse
from _routes._errors import HTTPError
from runtime_config.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def normalize_project_name(value: str | None) -> str | None:
    name = (value or "").strip()
    return name or None


class ProjectIngestHandler:
    def __init__(self, config: RuntimeConfig) -> None:
        self._dir = config.app_data_dir / "project_ingest"
        self._dir.mkdir(parents=True, exist_ok=True)

    def begin_from_generate(self, req: GenerateVideoRequest, job_id: str) -> None:
        self._write_from_generate(req, video_path="", job_id=job_id)

    def enqueue_from_generate(self, req: GenerateVideoRequest, video_path: str, job_id: str) -> None:
        self._write_from_generate(req, video_path=video_path, job_id=job_id)

    def drop_if_incomplete(self, job_id: str) -> None:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            return
        path = self._job_path(job_id)
        if not path.is_file():
            return
        try:
            job = ProjectIngestJob.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError):
            path.unlink(missing_ok=True)
            return
        if job.video_path:
            return
        path.unlink(missing_ok=True)

    def list_jobs(self) -> ProjectIngestListResponse:
        jobs: list[ProjectIngestJob] = []
        for path in self._dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                jobs.append(ProjectIngestJob.model_validate(payload))
            except (OSError, json.JSONDecodeError, ValueError):
                logger.warning("Skipping unreadable ingest job file: %s", path.name)
        # FIFO by enqueue time — never by job-id filename (random hex sorts wrong).
        jobs.sort(key=lambda job: (job.createdAt, job.id))
        return ProjectIngestListResponse(jobs=jobs)

    def delete_job(self, job_id: str) -> None:
        path = self._resolve_job_file(job_id)
        try:
            path.unlink()
        except FileNotFoundError:
            raise HTTPError(404, "Ingest job not found") from None

    def _write_from_generate(self, req: GenerateVideoRequest, *, video_path: str, job_id: str) -> None:
        project_name = normalize_project_name(req.projectName)
        if project_name is None:
            return
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            logger.warning("Skipping project ingest with invalid job id")
            return
        duration = None if req.duration is None else float(req.duration)
        created_at = time.time()
        existing_path = self._job_path(job_id)
        if existing_path.is_file():
            try:
                existing = ProjectIngestJob.model_validate(
                    json.loads(existing_path.read_text(encoding="utf-8"))
                )
                created_at = existing.createdAt
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        job = ProjectIngestJob(
            id=job_id,
            projectName=project_name,
            video_path=video_path,
            prompt=req.prompt,
            model=req.model,
            resolution=req.resolution,
            duration=duration,
            fps=int(req.fps),
            audio=req.audio,
            createdAt=created_at,
        )
        existing_path.write_text(job.model_dump_json(), encoding="utf-8")

    def _resolve_job_file(self, job_id: str) -> Path:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise HTTPError(400, "Invalid ingest job id")
        ingest_dir = self._dir.resolve()
        path = (ingest_dir / f"{job_id}.json").resolve()
        if not path.is_relative_to(ingest_dir):
            raise HTTPError(400, "Invalid ingest job id")
        if not path.is_file():
            raise HTTPError(404, "Ingest job not found")
        return path

    def _job_path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.json"
