"""In-memory FIFO for POST /api/generate. One worker; each request waits for its own job."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from _routes._errors import HTTPError
from api_types import GenerateVideoCancelledResponse, GenerateVideoRequest, GenerateVideoResponse

VIDEO_GENERATE_QUEUE_MAX = 32
_SLOT_RETRY_S = 0.05


@dataclass
class QueuedVideoGenerate:
    job_id: str
    req: GenerateVideoRequest
    done: threading.Event = field(default_factory=threading.Event)
    response: GenerateVideoResponse | None = None
    error: BaseException | None = None
    cancelled: bool = False


class VideoGenerateQueue:
    def __init__(
        self,
        run_job: Callable[[GenerateVideoRequest, str], GenerateVideoResponse],
        *,
        max_size: int = VIDEO_GENERATE_QUEUE_MAX,
    ) -> None:
        self._run_job = run_job
        self.max_size = max_size
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: deque[QueuedVideoGenerate] = deque()
        self._running: QueuedVideoGenerate | None = None
        self._thread: threading.Thread | None = None

    def submit(self, job_id: str, req: GenerateVideoRequest) -> GenerateVideoResponse:
        job = self._enqueue_locked(job_id, req)
        job.done.wait()
        if job.error is not None:
            raise job.error
        if job.response is None:
            raise HTTPError(500, "Video generation produced no response")
        return job.response

    def enqueue(self, job_id: str, req: GenerateVideoRequest) -> None:
        """Add to the FIFO without blocking — used for projectName ingest so HTTP
        connections are not held for the whole queue+GPU lifetime (browsers cap ~6)."""
        self._enqueue_locked(job_id, req)

    def _enqueue_locked(self, job_id: str, req: GenerateVideoRequest) -> QueuedVideoGenerate:
        job = QueuedVideoGenerate(job_id=job_id, req=req)
        with self._cv:
            occupied = len(self._pending) + (1 if self._running is not None else 0)
            if occupied >= self.max_size:
                raise HTTPError(429, "VIDEO_GENERATE_QUEUE_FULL")
            self._pending.append(job)
            self._ensure_worker_locked()
            self._cv.notify()
        return job

    def cancel_queued(self, job_id: str) -> bool:
        with self._cv:
            for i, job in enumerate(self._pending):
                if job.job_id != job_id:
                    continue
                del self._pending[i]
                job.cancelled = True
                job.response = GenerateVideoCancelledResponse(status="cancelled")
                job.done.set()
                return True
            return False

    def _ensure_worker_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._worker, name="video-generate-queue", daemon=True
            )
            self._thread.start()

    def _take_next(self) -> QueuedVideoGenerate:
        with self._cv:
            while not self._pending:
                self._cv.wait()
            return self._pending.popleft()

    def _worker(self) -> None:
        while True:
            job = self._take_next()
            if job.cancelled:
                continue
            with self._cv:
                self._running = job
            try:
                self._run_until_slot(job)
            finally:
                with self._cv:
                    self._running = None
                if not job.done.is_set():
                    if job.response is None and job.error is None:
                        job.response = GenerateVideoCancelledResponse(status="cancelled")
                    job.done.set()

    def _run_until_slot(self, job: QueuedVideoGenerate) -> None:
        while not job.cancelled:
            try:
                job.response = self._run_job(job.req, job.job_id)
                return
            except HTTPError as exc:
                if exc.status_code == 409 and exc.detail == "Generation already in progress":
                    time.sleep(_SLOT_RETRY_S)
                    continue
                job.error = exc
                return
            except Exception as exc:
                job.error = exc
                return
        job.response = GenerateVideoCancelledResponse(status="cancelled")
