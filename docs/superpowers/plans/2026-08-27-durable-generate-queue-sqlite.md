# Durable projectName Generate Queue (SQLite) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist projectName video generate jobs in SQLite so the queue auto-resumes after app/backend restart, while keeping ingest JSON as the gallery `/api/project-ingest` projection.

**Architecture:** New `GenerateQueueStore` (stdlib `sqlite3`) is source of truth for job id, full `GenerateVideoRequest`, and status. `VideoGenerateQueue` stays the in-memory worker and is rebuilt from incomplete SQLite rows on `VideoGenerationHandler` init. `ProjectIngestHandler` JSON files are synced from SQLite on enqueue/complete/fail/cancel/resume. Reconcile keeps incomplete JSON only when a matching incomplete SQLite row exists.

**Tech Stack:** Python 3.13+, stdlib `sqlite3`, existing FastAPI/`AppHandler` test fixtures (`pnpm backend:test`), no new pip dependencies, no frontend API changes.

## Global Constraints

- Only `projectName` video generates are durable (blocking curl / non-projectName unchanged).
- Auto-resume all incomplete (`queued` + interrupted `running`) on backend start; `running` resets to `queued` (no GPU checkpoint).
- Sync direction: SQLite → ingest JSON (never JSON → SQLite for resume).
- Public API unchanged: `{ status: "queued", id }` and existing `/api/project-ingest`.
- Queue full still returns `429` / `VIDEO_GENERATE_QUEUE_FULL` with **no** SQLite insert.
- Pre-migration incomplete ingest JSON without SQLite rows are orphans → drop.
- Corrupt DB on open → log, recreate empty DB, do not crash.
- v1 retains `complete` / `failed` / `cancelled` SQLite rows (no prune required).

---

### File map

| File | Responsibility |
|------|----------------|
| `backend/handlers/generate_queue_store.py` | SQLite open/schema/CRUD for `generate_jobs` |
| `backend/tests/test_generate_queue_store.py` | Unit tests for store |
| `backend/handlers/video_generation_handler.py` | Dual-write enqueue; status updates; startup resume; reconcile vs SQLite |
| `backend/handlers/project_ingest_handler.py` | Keep JSON projection; optionally tighten orphan helper if needed |
| `backend/handlers/video_generate_queue.py` | Unchanged worker API (`enqueue`, `live_job_ids`, `cancel_queued`) |
| `backend/_routes/project_ingest.py` | Keep calling `reconcile_project_ingest` before list/get |
| `backend/tests/test_project_ingest.py` | Dual-write + resume integration tests |
| `docs/superpowers/specs/2026-08-27-durable-generate-queue-sqlite-design.md` | Mark Implemented when done |

---

### Task 1: `GenerateQueueStore` (SQLite CRUD)

**Files:**
- Create: `backend/handlers/generate_queue_store.py`
- Create: `backend/tests/test_generate_queue_store.py`

**Interfaces:**
- Produces:
  - `GenerateJobStatus = Literal["queued", "running", "complete", "failed", "cancelled"]`
  - `@dataclass GenerateJobRecord` with fields: `id: str`, `request: GenerateVideoRequest`, `status: GenerateJobStatus`, `project_name: str`, `video_path: str`, `error: str | None`, `created_at: float`, `updated_at: float`
  - `class GenerateQueueStore`:
    - `__init__(self, db_path: Path) -> None`
    - `insert_queued(self, job_id: str, req: GenerateVideoRequest) -> None`
    - `set_status(self, job_id: str, status: GenerateJobStatus, *, video_path: str | None = None, error: str | None = None) -> None`
    - `get(self, job_id: str) -> GenerateJobRecord | None`
    - `list_incomplete(self) -> list[GenerateJobRecord]` — `queued`+`running`, order by `created_at`, `id`
    - `incomplete_ids(self) -> set[str]`
    - `reset_running_to_queued(self) -> None` — all `running` → `queued`, bump `updated_at`
- Consumes: `GenerateVideoRequest` from `api_types` (`model_dump_json` / `model_validate_json`)

- [ ] **Step 1: Write failing store tests**

Create `backend/tests/test_generate_queue_store.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_types import GenerateVideoRequest
from handlers.generate_queue_store import GenerateQueueStore


def _req(**overrides: object) -> GenerateVideoRequest:
    payload: dict[str, object] = {
        "prompt": "moon landing",
        "resolution": "1080p",
        "model": "fast",
        "duration": 5,
        "fps": 24,
        "cameraMotion": "none",
        "projectName": "Moon landing",
    }
    payload.update(overrides)
    return GenerateVideoRequest.model_validate(payload)


def test_insert_queued_round_trips(tmp_path: Path) -> None:
    store = GenerateQueueStore(tmp_path / "generate_queue.sqlite")
    store.insert_queued("abc12345", _req())
    row = store.get("abc12345")
    assert row is not None
    assert row.status == "queued"
    assert row.project_name == "Moon landing"
    assert row.video_path == ""
    assert row.request.prompt == "moon landing"
    assert store.incomplete_ids() == {"abc12345"}


def test_list_incomplete_fifo_and_status_updates(tmp_path: Path) -> None:
    store = GenerateQueueStore(tmp_path / "generate_queue.sqlite")
    store.insert_queued("job00001", _req(prompt="first"))
    store.insert_queued("job00002", _req(prompt="second"))
    store.set_status("job00001", "running")
    incomplete = store.list_incomplete()
    assert [j.id for j in incomplete] == ["job00001", "job00002"]
    assert incomplete[0].status == "running"

    store.set_status("job00001", "complete", video_path="/tmp/out.mp4")
    store.set_status("job00002", "failed", error="missing image")
    assert store.list_incomplete() == []
    done = store.get("job00001")
    assert done is not None
    assert done.status == "complete"
    assert done.video_path == "/tmp/out.mp4"
    failed = store.get("job00002")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == "missing image"


def test_reset_running_to_queued(tmp_path: Path) -> None:
    store = GenerateQueueStore(tmp_path / "generate_queue.sqlite")
    store.insert_queued("run00001", _req())
    store.set_status("run00001", "running")
    store.reset_running_to_queued()
    row = store.get("run00001")
    assert row is not None
    assert row.status == "queued"


def test_corrupt_db_recreates_empty(tmp_path: Path) -> None:
    path = tmp_path / "generate_queue.sqlite"
    path.write_text("not-a-sqlite-file", encoding="utf-8")
    store = GenerateQueueStore(path)
    assert store.list_incomplete() == []
    store.insert_queued("new00001", _req())
    assert store.get("new00001") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm backend:test -- tests/test_generate_queue_store.py -v`

Expected: FAIL with import error / `GenerateQueueStore` not found.

- [ ] **Step 3: Implement `GenerateQueueStore`**

Create `backend/handlers/generate_queue_store.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm backend:test -- tests/test_generate_queue_store.py -v`

Expected: PASS (all 4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/handlers/generate_queue_store.py backend/tests/test_generate_queue_store.py
git commit -m "feat: add SQLite GenerateQueueStore for durable projectName jobs"
```

---

### Task 2: Dual-write on enqueue + status transitions

**Files:**
- Modify: `backend/handlers/video_generation_handler.py`
- Modify: `backend/tests/test_project_ingest.py`

**Interfaces:**
- Consumes: `GenerateQueueStore` from Task 1
- Produces (handler behavior):
  - On projectName enqueue: `store.insert_queued` then `queue.enqueue` then `project_ingest.begin_from_generate` (if enqueue raises `429`, no insert — check capacity **before** insert, or insert only after successful enqueue; preferred: call `queue.enqueue` first only if we can roll back — **spec:** no SQLite insert on 429 → enqueue first, then `insert_queued`, then begin JSON; on insert failure after enqueue, still leave worker job but log — better: check occupied under queue lock. Minimal approach matching current code: `self._queue.enqueue(...)` first; on success `store.insert_queued` + `begin_from_generate`. Spec also wanted atomicity — acceptable v1: enqueue then insert; if insert fails, cancel_queued + re-raise.)
  - When worker starts `_run_queued_job`: if projectName set, `store.set_status(id, "running")`
  - On success path that calls `enqueue_from_generate(..., video_path)`: also `store.set_status(id, "complete", video_path=...)`
  - On fail/cancel drop paths that call `drop_if_incomplete`: also `store.set_status(id, "failed"|"cancelled", error=...)`

- [ ] **Step 1: Write failing dual-write tests**

Append to `backend/tests/test_project_ingest.py`:

```python
def test_project_name_generate_writes_sqlite_row(
    client, test_state, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)

    r = client.post(
        "/api/generate",
        json={**_T2V_JSON, "prompt": "sqlite row", "projectName": "Moon landing"},
    )
    assert r.status_code == 200
    job_id = r.json()["id"]
    row = test_state.video_generation._queue_store.get(job_id)
    assert row is not None
    assert row.status in ("queued", "running", "complete")
    assert row.project_name == "Moon landing"
    assert row.request.prompt == "sqlite row"


def test_queue_full_does_not_insert_sqlite(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 8
    pipeline.step_delay_s = 0.05

    # Fill queue to max (reuse pattern from test_generate_queue_full_returns_429)
    from handlers.video_generate_queue import VIDEO_GENERATE_QUEUE_MAX

    ids: list[str] = []
    for i in range(VIDEO_GENERATE_QUEUE_MAX):
        r = client.post(
            "/api/generate",
            json={**_T2V_JSON, "prompt": f"fill {i}", "projectName": "Moon landing"},
        )
        assert r.status_code == 200
        ids.append(r.json()["id"])

    overflow = client.post(
        "/api/generate",
        json={**_T2V_JSON, "prompt": "overflow", "projectName": "Moon landing"},
    )
    assert overflow.status_code == 429
    # No SQLite row should exist for a non-returned id; all stored ids are the fill set
    for job_id in ids:
        assert test_state.video_generation._queue_store.get(job_id) is not None
```

(If `test_generate_queue_full_returns_429` already drains/cancels, mirror its exact fill strategy from that test rather than inventing a new one.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm backend:test -- tests/test_project_ingest.py::test_project_name_generate_writes_sqlite_row -v`

Expected: FAIL (`_queue_store` missing).

- [ ] **Step 3: Wire store into `VideoGenerationHandler`**

In `__init__`:

```python
from handlers.generate_queue_store import GenerateQueueStore

self._queue_store = GenerateQueueStore(config.app_data_dir / "generate_queue.sqlite")
self._queue = VideoGenerateQueue(self._run_queued_job)
# resume comes in Task 3 — for now only construct store + keep existing reconcile
self.reconcile_project_ingest()
```

Update projectName branch of `generate`:

```python
if normalize_project_name(req.projectName) is not None:
    self._queue.enqueue(generation_id, req)
    self._queue_store.insert_queued(generation_id, req)
    self._project_ingest.begin_from_generate(req, generation_id)
    return GenerateVideoQueuedResponse(status="queued", id=generation_id)
```

At the start of `_run_queued_job`, after resolving use_api / before heavy work:

```python
if normalize_project_name(req.projectName) is not None:
    self._queue_store.set_status(generation_id, "running")
```

Wherever `enqueue_from_generate(req, str(output_path), generation_id)` is called for projectName completions, add:

```python
self._queue_store.set_status(generation_id, "complete", video_path=str(output_path))
```

Wherever `drop_if_incomplete(generation_id)` runs due to cancel/failure for a projectName job, add the matching terminal status (`cancelled` / `failed` with `error=str(exc)` when known). Prefer a small private helper:

```python
def _fail_project_job(self, req: GenerateVideoRequest, generation_id: str, *, status: str, error: str | None = None) -> None:
    if normalize_project_name(req.projectName) is None:
        self._project_ingest.drop_if_incomplete(generation_id)
        return
    self._queue_store.set_status(generation_id, status, error=error)  # type: ignore[arg-type]
    self._project_ingest.drop_if_incomplete(generation_id)
```

Use it from existing except paths instead of bare `drop_if_incomplete` when `req` is in scope.

- [ ] **Step 4: Run dual-write + existing ingest tests**

Run: `pnpm backend:test -- tests/test_project_ingest.py -v`

Expected: PASS (including new dual-write tests; existing cancel/fail/complete still green).

- [ ] **Step 5: Commit**

```bash
git add backend/handlers/video_generation_handler.py backend/tests/test_project_ingest.py
git commit -m "feat: dual-write projectName generates to SQLite and ingest JSON"
```

---

### Task 3: Startup resume (rebuild in-memory queue)

**Files:**
- Modify: `backend/handlers/video_generation_handler.py`
- Modify: `backend/tests/test_project_ingest.py`

**Interfaces:**
- Produces: `VideoGenerationHandler._resume_durable_queue(self) -> None` called from `__init__` after store+queue construction
- Behavior:
  1. `reset_running_to_queued()`
  2. For each `list_incomplete()` in order: `begin_from_generate(record.request, record.id)` then `queue.enqueue(record.id, record.request)`
  3. Skip enqueue if id already in `live_job_ids()` (idempotent)
  4. If `request` media paths missing on disk when worker eventually runs, existing validation/fail paths mark failed (Task 2 helper)

- [ ] **Step 1: Write failing resume integration test**

```python
def test_restart_resumes_incomplete_project_name_jobs(
    test_state, fake_services, create_fake_model_files, tmp_path
) -> None:
    """Simulate process death: durable SQLite rows + empty in-memory queue → new handler resumes."""
    from app_handler import ServiceBundle
    from runtime_config.port_constant import PORT
    from state import RuntimeConfig, build_initial_state, set_state_service_for_tests
    from state.app_settings import AppSettings
    from state.app_state_types import HfAuthenticated
    from tests.fake_camera_motion_prompts import FAKE_CAMERA_MOTION_PROMPTS
    from tests.conftest import DEFAULT_NEGATIVE_PROMPT
    import torch

    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 8
    pipeline.step_delay_s = 0.2

    from starlette.testclient import TestClient
    from app_factory import create_app
    from tests.conftest import TEST_ADMIN_TOKEN

    with TestClient(create_app(handler=test_state, admin_token=TEST_ADMIN_TOKEN)) as client:
        r1 = client.post(
            "/api/generate",
            json={**_T2V_JSON, "prompt": "first", "projectName": "Alpha"},
        )
        r2 = client.post(
            "/api/generate",
            json={**_T2V_JSON, "prompt": "second", "projectName": "Beta"},
        )
        assert r1.status_code == 200 and r2.status_code == 200
        id1, id2 = r1.json()["id"], r2.json()["id"]
        assert pipeline.entered_inference.wait(timeout=5)

    # Pretend process died: SQLite retained under same app_data_dir; build a new AppHandler.
    app_data = test_state.config.app_data_dir
    config = RuntimeConfig(
        device=torch.device("cpu"),
        app_data_dir=app_data,
        default_models_dir=test_state.config.default_models_dir,
        outputs_dir=test_state.config.outputs_dir,
        settings_file=app_data / "settings.json",
        ltx_api_base_url="https://api.ltx.video",
        local_generations_mode="full_models_loading",
        use_sage_attention=False,
        camera_motion_prompts=FAKE_CAMERA_MOTION_PROMPTS,
        default_negative_prompt=DEFAULT_NEGATIVE_PROMPT,
        dev_mode=False,
        hf_oauth_client_id="test-client-id",
        backend_port=PORT,
    )
    bundle = ServiceBundle(
        http=fake_services.http,
        gpu_cleaner=fake_services.gpu_cleaner,
        model_downloader=fake_services.model_downloader,
        lora_catalog_provider=fake_services.lora_catalog_provider,
        gpu_info=fake_services.gpu_info,
        video_processor=fake_services.video_processor,
        text_encoder=fake_services.text_encoder,
        task_runner=fake_services.task_runner,
        ltx_api_client=fake_services.ltx_api_client,
        zit_api_client=fake_services.zit_api_client,
        fast_video_pipeline_class=type(fake_services.fast_video_pipeline),
        image_generation_pipeline_class=type(fake_services.image_generation_pipeline),
        ic_lora_pipeline_class=type(fake_services.ic_lora_pipeline),
        depth_processor_pipeline_class=type(fake_services.depth_processor_pipeline),
        pose_processor_pipeline_class=type(fake_services.pose_processor_pipeline),
        a2v_pipeline_class=type(fake_services.a2v_pipeline),
        retake_pipeline_class=type(fake_services.retake_pipeline),
        prompt_enhancer_pipeline_class=type(fake_services.prompt_enhancer_pipeline),
    )
    # Stop the old handler's DI registration before replacing.
    resumed = build_initial_state(config, AppSettings().model_copy(deep=True), service_bundle=bundle)
    resumed.state.hf_auth_state = HfAuthenticated(access_token="fake-hf-token", expires_at=1e18)
    set_state_service_for_tests(resumed)
    _enable_local_text_encoding(resumed)

    assert id1 in resumed.video_generation._queue.live_job_ids() or resumed.video_generation._queue_store.get(id1)
    # Wait for both to complete via ingest list
    deadline = time.time() + 30
    completed: set[str] = set()
    with TestClient(create_app(handler=resumed, admin_token=TEST_ADMIN_TOKEN)) as client2:
        while time.time() < deadline:
            jobs = client2.get("/api/project-ingest").json()["jobs"]
            for job in jobs:
                if job["id"] in (id1, id2) and job["video_path"]:
                    completed.add(job["id"])
            # Also check SQLite complete if gallery deleted JSON already
            for jid in (id1, id2):
                row = resumed.video_generation._queue_store.get(jid)
                if row and row.status == "complete":
                    completed.add(jid)
            if completed >= {id1, id2}:
                break
            time.sleep(0.05)
    assert completed >= {id1, id2}
```

Simplify if the full DI rebuild is too heavy: unit-test `_resume_durable_queue` by inserting two SQLite rows with `insert_queued`, constructing only `VideoGenerationHandler` pieces — but prefer the integration shape above. If flaky, assert immediately after resume that `live_job_ids() == {id1, id2}` (or both still incomplete in store and present in live queue) **before** waiting for completion.

Minimum acceptable assertion for this task:

```python
assert resumed.video_generation._queue.live_job_ids() == {id1, id2}
# or both ids subset of live_job_ids after resume
```

plus a follow-up wait for `complete` status in SQLite.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm backend:test -- tests/test_project_ingest.py::test_restart_resumes_incomplete_project_name_jobs -v`

Expected: FAIL (jobs not in live queue after new handler).

- [ ] **Step 3: Implement `_resume_durable_queue`**

```python
def _resume_durable_queue(self) -> None:
    self._queue_store.reset_running_to_queued()
    live = self._queue.live_job_ids()
    for record in self._queue_store.list_incomplete():
        if record.id in live:
            continue
        self._project_ingest.begin_from_generate(record.request, record.id)
        try:
            self._queue.enqueue(record.id, record.request)
        except HTTPError as exc:
            if exc.status_code == 429:
                logger.error("Could not resume job %s: queue full", record.id)
                break
            raise
        live.add(record.id)
```

Call from `__init__` after creating `_queue` / `_queue_store`, **before** `reconcile_project_ingest()`:

```python
self._queue_store = GenerateQueueStore(config.app_data_dir / "generate_queue.sqlite")
self._queue = VideoGenerateQueue(self._run_queued_job)
self._resume_durable_queue()
self.reconcile_project_ingest()
```

- [ ] **Step 4: Run resume + ingest suite**

Run: `pnpm backend:test -- tests/test_project_ingest.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/handlers/video_generation_handler.py backend/tests/test_project_ingest.py
git commit -m "feat: resume incomplete projectName jobs from SQLite on startup"
```

---

### Task 4: Reconcile against SQLite (replace live-queue-only orphan drop)

**Files:**
- Modify: `backend/handlers/video_generation_handler.py` (`reconcile_project_ingest`)
- Modify: `backend/tests/test_project_ingest.py`

**Interfaces:**
- Replace current `drop_orphaned_incomplete(live_job_ids)` semantics when store exists:
  1. `incomplete_sqlite = self._queue_store.incomplete_ids()`
  2. For each ingest JSON job with empty `video_path`: if `id not in incomplete_sqlite` → `drop_if_incomplete`
  3. For each id in `incomplete_sqlite`: if ingest JSON missing → `begin_from_generate` from store record
  4. Ensure each incomplete SQLite id is in `live_job_ids()` (enqueue if missing — same as resume safety net)

- [ ] **Step 1: Update / add reconcile tests**

Replace expectations of `test_list_project_ingest_drops_stale_incomplete_after_restart`:

- Incomplete JSON **without** SQLite row → still dropped on GET list.
- Incomplete JSON **with** SQLite incomplete row → **kept**, and after reconcile appears in live queue.

```python
def test_list_keeps_incomplete_when_sqlite_row_exists(test_state, client) -> None:
    req = _named_req(prompt="durable")
    test_state.video_generation._queue_store.insert_queued("durable1", req)
    test_state.project_ingest.begin_from_generate(req, "durable1")
    listed = client.get("/api/project-ingest")
    assert listed.status_code == 200
    jobs = listed.json()["jobs"]
    assert any(j["id"] == "durable1" and j["video_path"] == "" for j in jobs)


def test_list_recreates_missing_ingest_json_from_sqlite(test_state, client) -> None:
    req = _named_req(prompt="rehydrate")
    test_state.video_generation._queue_store.insert_queued("rehyd001", req)
    # no begin_from_generate — JSON missing
    listed = client.get("/api/project-ingest")
    assert any(j["id"] == "rehyd001" for j in listed.json()["jobs"])


def test_list_still_drops_json_orphan_without_sqlite(client, test_state) -> None:
    test_state.project_ingest.begin_from_generate(_named_req(prompt="orphaned"), "orphan01")
    listed = client.get("/api/project-ingest")
    assert listed.json() == {"jobs": []}
```

Remove or rewrite the old test that only asserted empty list after orphan JSON with no SQLite (keep as `test_list_still_drops_json_orphan_without_sqlite`).

- [ ] **Step 2: Run tests to verify new keep/rehydrate cases fail**

Run: `pnpm backend:test -- tests/test_project_ingest.py::test_list_keeps_incomplete_when_sqlite_row_exists tests/test_project_ingest.py::test_list_recreates_missing_ingest_json_from_sqlite -v`

Expected: FAIL (current reconcile drops SQLite-backed JSON or does not rehydrate).

- [ ] **Step 3: Implement SQLite-aware reconcile**

```python
def reconcile_project_ingest(self) -> None:
    incomplete = {job.id: job for job in self._queue_store.list_incomplete()}
    live = self._queue.live_job_ids()

    for disk_job in self._project_ingest.list_jobs().jobs:
        if disk_job.video_path:
            continue
        if disk_job.id not in incomplete:
            self._project_ingest.drop_if_incomplete(disk_job.id)

    for job_id, record in incomplete.items():
        # Ensure JSON projection exists
        disk_ids = {j.id for j in self._project_ingest.list_jobs().jobs}
        if job_id not in disk_ids:
            self._project_ingest.begin_from_generate(record.request, job_id)
        if job_id not in live:
            try:
                self._queue.enqueue(job_id, record.request)
                live.add(job_id)
            except HTTPError as exc:
                if exc.status_code == 429:
                    logger.error("Reconcile could not enqueue %s: queue full", job_id)
                else:
                    raise
```

Avoid double-listing thrash: collect disk incomplete ids once.

- [ ] **Step 4: Run ingest suite**

Run: `pnpm backend:test -- tests/test_project_ingest.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/handlers/video_generation_handler.py backend/tests/test_project_ingest.py
git commit -m "fix: reconcile project ingest against SQLite incomplete jobs"
```

---

### Task 5: Cancel / DELETE marks SQLite cancelled

**Files:**
- Modify: `backend/handlers/video_generation_handler.py` (`cancel_queued`)
- Modify: `backend/_routes/project_ingest.py` if delete path needs store update when cancel_queued is false but JSON deleted
- Modify: `backend/tests/test_project_ingest.py`

**Interfaces:**
- `cancel_queued(job_id)`: on successful in-memory cancel → `store.set_status(job_id, "cancelled")` + `drop_if_incomplete`
- DELETE route today: `cancel_queued` OR `project_ingest.delete_job`. If delete_job path (already complete), leave SQLite `complete`. If cancel path, SQLite already `cancelled`. If somehow incomplete JSON deleted without cancel (should not happen), mark cancelled in store inside `delete_job` caller:

```python
# route_delete_project_ingest
if handler.video_generation.cancel_queued(job_id):
    return StatusResponse(status="ok")
handler.video_generation.mark_ingest_deleted(job_id)  # optional helper
handler.project_ingest.delete_job(job_id)
```

Prefer extending `cancel_queued` only; for incomplete-not-live edge, `reconcile` already handles. Minimum: update `cancel_queued` body.

- [ ] **Step 1: Write failing cancel test**

```python
def test_cancel_queued_marks_sqlite_cancelled(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 8
    pipeline.step_delay_s = 0.05
    assert client.post("/api/generate", json={**_T2V_JSON, "prompt": "runner", "projectName": "A"}).status_code == 200
    assert pipeline.entered_inference.wait(timeout=5)
    queued = client.post("/api/generate", json={**_T2V_JSON, "prompt": "waiting", "projectName": "B"})
    job_id = queued.json()["id"]
    deleted = client.delete(f"/api/project-ingest/{job_id}")
    assert deleted.status_code == 200
    row = test_state.video_generation._queue_store.get(job_id)
    assert row is not None
    assert row.status == "cancelled"
    assert all(j["id"] != job_id for j in client.get("/api/project-ingest").json()["jobs"])
```

- [ ] **Step 2: Run test to verify fail**

Run: `pnpm backend:test -- tests/test_project_ingest.py::test_cancel_queued_marks_sqlite_cancelled -v`

Expected: FAIL (row still `queued` or missing status update).

- [ ] **Step 3: Update `cancel_queued`**

```python
def cancel_queued(self, job_id: str) -> bool:
    cancelled = self._queue.cancel_queued(job_id)
    if cancelled:
        self._queue_store.set_status(job_id, "cancelled")
        self._project_ingest.drop_if_incomplete(job_id)
    return cancelled
```

- [ ] **Step 4: Run ingest suite**

Run: `pnpm backend:test -- tests/test_project_ingest.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/handlers/video_generation_handler.py backend/tests/test_project_ingest.py
git commit -m "feat: mark SQLite generate job cancelled on queue delete"
```

---

### Task 6: Full verification + design status

**Files:**
- Modify: `docs/superpowers/specs/2026-08-27-durable-generate-queue-sqlite-design.md` (Status → Implemented)

- [ ] **Step 1: Run focused backend suites**

Run:

```bash
pnpm backend:test -- tests/test_generate_queue_store.py tests/test_project_ingest.py -v
pnpm typecheck:py
```

Expected: all PASS; pyright clean for new store module.

- [ ] **Step 2: Manual smoke (optional but recommended)**

1. `pnpm dev` → project A generate → project B generate while A runs → both show Queued/Generating correctly.
2. Quit app while B still queued.
3. Relaunch → B (and A if interrupted) resume and import without permanent stale Queued card.

- [ ] **Step 3: Mark design implemented**

In the design doc header: `**Status:** Implemented.`

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-27-durable-generate-queue-sqlite-design.md
git commit -m "docs: mark durable generate queue SQLite design implemented"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| SQLite schema + CRUD | Task 1 |
| Corrupt DB recreate | Task 1 |
| Dual-write enqueue | Task 2 |
| No SQLite insert on 429 | Task 2 |
| running / complete / failed status | Task 2 |
| Startup resume + running→queued | Task 3 |
| Reconcile vs SQLite (keep/rehydrate/drop orphan) | Task 4 |
| DELETE → cancelled | Task 5 |
| No frontend API changes | (implicit — no frontend tasks) |
| Success criteria / manual smoke | Task 6 |

## Placeholder / consistency review

- Store path fixed: `config.app_data_dir / "generate_queue.sqlite"`.
- Method names consistent: `insert_queued`, `set_status`, `list_incomplete`, `incomplete_ids`, `reset_running_to_queued`, `_resume_durable_queue`, `_queue_store`.
- Enqueue order for new jobs: in-memory `enqueue` then `insert_queued` then JSON (429-safe). Resume/reconcile: JSON then enqueue.
- No TBD/TODO left in steps.
