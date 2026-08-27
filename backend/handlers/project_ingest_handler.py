"""Persist pending project-ingest jobs for the Electron gallery watcher."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from api_types import (
    GenerateVideoRequest,
    ProjectIngestJob,
    ProjectIngestJobDetailResponse,
    ProjectIngestListResponse,
)
from _routes._errors import HTTPError
from handlers.outputs_handler import public_output_url
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
        # Completed jobs live here after the desktop gallery imports + deletes them,
        # so GET /api/project-ingest/{id} still returns video_url for curl/API clients.
        self._done_dir = self._dir / "done"
        self._done_dir.mkdir(parents=True, exist_ok=True)

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

    def drop_orphaned_incomplete(self, live_job_ids: set[str]) -> None:
        """Remove incomplete disk jobs that are not in the live generate queue.

        Project-ingest JSON is durable across process death; VideoGenerateQueue is not.
        After an app/backend restart, incomplete files with empty video_path would otherwise
        linger forever as "queued" with nothing to run them.
        """
        for job in self.list_jobs().jobs:
            if job.video_path:
                continue
            if job.id in live_job_ids:
                continue
            self.drop_if_incomplete(job.id)

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

    def get_job(self, job_id: str) -> ProjectIngestJobDetailResponse:
        path = self._resolve_job_file(job_id)
        try:
            job = ProjectIngestJob.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPError(404, "Ingest job not found") from exc
        if job.video_path:
            return ProjectIngestJobDetailResponse(
                id=job.id,
                projectName=job.projectName,
                video_path=job.video_path,
                video_url=public_output_url(job.video_path),
                status="complete",
                prompt=job.prompt,
                model=job.model,
                resolution=job.resolution,
                duration=job.duration,
                fps=job.fps,
                audio=job.audio,
                createdAt=job.createdAt,
            )
        return ProjectIngestJobDetailResponse(
            id=job.id,
            projectName=job.projectName,
            video_path="",
            video_url=None,
            status="queued",
            prompt=job.prompt,
            model=job.model,
            resolution=job.resolution,
            duration=job.duration,
            fps=job.fps,
            audio=job.audio,
            createdAt=job.createdAt,
        )

    def delete_job(self, job_id: str) -> None:
        path = self._resolve_active_job_file(job_id)
        try:
            raw = path.read_text(encoding="utf-8")
            job = ProjectIngestJob.model_validate(json.loads(raw))
            if job.video_path:
                archive = self._done_job_path(job_id)
                archive.write_text(raw, encoding="utf-8")
            path.unlink()
        except FileNotFoundError:
            raise HTTPError(404, "Ingest job not found") from None
        except (OSError, json.JSONDecodeError, ValueError):
            path.unlink(missing_ok=True)

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
        """Active pending job, or archived completed job after gallery import."""
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise HTTPError(400, "Invalid ingest job id")
        for base in (self._dir, self._done_dir):
            root = base.resolve()
            path = (root / f"{job_id}.json").resolve()
            if not path.is_relative_to(root):
                raise HTTPError(400, "Invalid ingest job id")
            if path.is_file():
                return path
        raise HTTPError(404, "Ingest job not found")

    def _resolve_active_job_file(self, job_id: str) -> Path:
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

    def _done_job_path(self, job_id: str) -> Path:
        return self._done_dir / f"{job_id}.json"
