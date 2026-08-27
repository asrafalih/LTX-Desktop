# Durable projectName generate queue (SQLite + ingest JSON) — design

**Date:** 2026-08-27  
**Status:** Approved for planning.

## Problem

ProjectName / GenSpace video jobs use an in-memory FIFO (`VideoGenerateQueue`) plus durable ingest JSON files under `app_data_dir/project_ingest/`. After an app or backend restart:

- Incomplete ingest JSON files remain on disk.
- The in-memory queue is empty and is not rebuilt.
- Home / GenSpace show **Queued** forever even though nothing will run.

A recent reconcile path drops incomplete ingest files that are not in the live queue (correct for “stale card” cleanup, but it does not resume work).

## Goals

- Persist the **projectName** video generate queue across backend restarts.
- **Auto-resume** all incomplete jobs on startup (jobs that were `queued` or interrupted while `running`).
- Keep `/api/project-ingest` and the Electron gallery watcher working against **ingest JSON** (minimal frontend change).
- Keep the public generate API shape: `{ status: "queued", id }` for projectName generates.

## Non-goals (v1)

- Persisting non-`projectName` (blocking curl) generates.
- Persisting retake / extend / image / IC-LoRA jobs.
- Moving projects, settings, or assets into SQLite.
- Checkpointing mid-GPU progress (interrupted `running` jobs restart from scratch).
- User-facing “Resume queue?” prompt (auto-resume only).

## Approach

**SQLite owns the queue; ingest JSON remains the gallery/API projection.**

| Store | Role |
|---|---|
| SQLite (`app_data_dir/generate_queue.sqlite`) | Source of truth for projectName job identity, full request payload, status, resume |
| Ingest JSON (`project_ingest/*.json`) | Projection for `/api/project-ingest` + gallery import watcher |
| In-memory `VideoGenerateQueue` | Worker FIFO only; rebuilt from SQLite on startup |

Direction of sync: **SQLite → ingest JSON** on enqueue / status transitions / complete. Not the reverse.

## Data model

Table `generate_jobs` (name flexible):

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | Same generation id as today |
| `request_json` | TEXT | Full `GenerateVideoRequest` for resume |
| `status` | TEXT | `queued` \| `running` \| `complete` \| `failed` \| `cancelled` |
| `project_name` | TEXT | Denormalized for listing / sync |
| `video_path` | TEXT | Empty until complete |
| `error` | TEXT NULL | Failure reason when `failed` |
| `created_at` | REAL | FIFO ordering |
| `updated_at` | REAL | Debugging / tooling |

Library: Python stdlib `sqlite3` (no new runtime dependency). Schema created on first open.

## Job lifecycle

### Enqueue (`POST /api/generate` with `projectName`)

1. Insert SQLite row: `status=queued`, store full request, `video_path=""` — and add to the in-memory worker FIFO in the same critical section (so list/reconcile never sees a durable row without a live queue entry, or the reverse).
2. Sync ingest JSON (`begin_from_generate` equivalent). Brief window with SQLite+worker but no JSON is OK; startup/list recreates JSON from SQLite if needed.
3. Return `GenerateVideoQueuedResponse` (unchanged).

If the live enqueue would exceed `VIDEO_GENERATE_QUEUE_MAX`, raise `429` and do **not** insert the SQLite row.

### Start running

- SQLite → `running`.
- Ingest JSON stays incomplete (`video_path=""`). Home / GenSpace continue to use generation progress id for Generating vs Queued.

### Complete

- SQLite → `complete` + `video_path`.
- Sync ingest JSON with path (gallery imports, then deletes JSON as today).
- After gallery delete: keep SQLite row as `complete` for history, or delete/archive once import is confirmed. **v1 recommendation:** leave `complete` rows in SQLite; optional later prune. Ingest `done/` archive behavior can remain as today for GET-by-id after gallery delete.

### Cancel / fail

- SQLite → `cancelled` / `failed` (set `error` when failed). Keep the row (same as `complete` retention in v1).
- Drop incomplete ingest JSON.
- Remove from in-memory pending if still queued; running cancel uses existing cancel paths.

### Startup resume

1. Open DB; ensure schema.
2. Load rows with `status IN ('queued', 'running')` ordered by `created_at`, then `id`.
3. Reset any `running` → `queued` (no GPU checkpoint).
4. Sync ingest JSON for each incomplete row.
5. Rebuild in-memory FIFO and start the worker.
6. Frontend needs no special resume UI beyond existing Queued / Generating.

## Reconcile / orphan rules (update)

Replace “drop every incomplete ingest file not in the live in-memory queue” with:

| Situation | Action |
|---|---|
| Incomplete ingest JSON **with** matching incomplete SQLite row | Keep; ensure job is in live queue (or will be after startup resume) |
| Incomplete ingest JSON **without** SQLite row | Drop (true orphan; e.g. pre-migration leftovers) |
| SQLite incomplete **without** ingest JSON | Recreate ingest JSON from SQLite on list/startup |
| SQLite `complete` / `failed` / `cancelled` with stale incomplete JSON | Drop incomplete JSON |

## Errors / edge cases

- **Corrupt DB on startup:** log error; recreate empty DB; do not crash the app. Orphan ingest JSON without SQLite rows is dropped.
- **Resume with missing media paths** (image/audio): mark `failed`, set `error`, drop incomplete ingest JSON, continue with next job.
- **Queue full (`429`):** unchanged — do not insert SQLite row if the live enqueue is rejected.
- **DELETE `/api/project-ingest/{id}`:** cancel live queued job if present; mark SQLite `cancelled`; delete ingest JSON. UX unchanged.

## Migration

- First run creates the SQLite file and schema.
- Existing incomplete ingest JSON from before this feature **cannot** be resumed (no stored request payload) → treat as orphans and drop (same user-visible outcome as current restart cleanup).
- Every new projectName generate writes SQLite going forward.

## API / frontend

- No new public endpoints required for v1.
- `/api/project-ingest` continues to list/get/delete ingest JSON (after reconcile against SQLite).
- Home / GenSpace keep current polling and Queued vs Generating logic.

## Testing

- Unit: enqueue writes SQLite + JSON; complete syncs path; cancel/fail cleans both stores.
- Integration: enqueue two projectName jobs → destroy handler / new `AppHandler` → both resume and complete in FIFO order.
- Integration: crash while `running` → row resets to `queued` and re-runs successfully.
- Integration: incomplete JSON without SQLite row is dropped on list/reconcile.
- Update existing `test_project_ingest.py` coverage for the dual-write path.

## Components (implementation sketch)

- `backend/handlers/generate_queue_store.py` (or similar): open DB, migrate schema, CRUD, list incomplete.
- Wire into `VideoGenerationHandler`: write/update on enqueue/run/complete/fail/cancel; `reconcile_project_ingest` consults SQLite; startup calls resume.
- `ProjectIngestHandler`: remains JSON projection; called to sync from SQLite-driven transitions.
- `VideoGenerateQueue`: stays in-memory worker; gains rebuild-from-list on startup via handler.

## Success criteria

- Close app with one generating + one queued projectName job → reopen → both eventually complete and import into the correct projects.
- No permanent stale “Queued” Home card for jobs that will never run.
- Existing GenSpace queue UI and `/api/project-ingest` curl flows keep working without frontend API changes.
