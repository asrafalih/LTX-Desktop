"""Tests for optional generate projectName ingest queue."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from _routes._errors import HTTPError
from api_types import GenerateVideoRequest
from services.generation_interrupt import GenerationCancelledError
from tests.http_error_assertions import assert_http_error
from tests.test_generation import _T2V_JSON, _enable_local_text_encoding


def test_generate_without_project_name_does_not_enqueue(client, test_state, create_fake_model_files) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)

    r = client.post(
        "/api/generate",
        json={
            "prompt": "A beautiful sunset",
            "resolution": "1080p",
            "model": "fast",
            "duration": 5,
            "fps": 24,
            "cameraMotion": "none",
        },
    )
    assert r.status_code == 200
    listed = client.get("/api/project-ingest")
    assert listed.status_code == 200
    assert listed.json() == {"jobs": []}


def test_generate_with_blank_project_name_does_not_enqueue(
    client, test_state, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)

    r = client.post(
        "/api/generate",
        json={
            "prompt": "A beautiful sunset",
            "resolution": "1080p",
            "model": "fast",
            "duration": 5,
            "fps": 24,
            "cameraMotion": "none",
            "projectName": "   ",
        },
    )
    assert r.status_code == 200
    listed = client.get("/api/project-ingest")
    assert listed.json() == {"jobs": []}


def test_generate_with_project_name_enqueues_and_delete_consumes(
    client, test_state, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)

    r = client.post(
        "/api/generate",
        json={
            "prompt": "moon landing",
            "resolution": "1080p",
            "model": "fast",
            "duration": 5,
            "fps": 24,
            "cameraMotion": "none",
            "projectName": "Moon landing",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    job_id = r.json()["id"]
    assert job_id

    deadline = time.time() + 15
    job: dict[str, object] | None = None
    while time.time() < deadline:
        listed = client.get("/api/project-ingest")
        assert listed.status_code == 200
        jobs = listed.json()["jobs"]
        match = next((j for j in jobs if j["id"] == job_id), None)
        if match and match["video_path"]:
            job = match
            break
        time.sleep(0.05)
    assert job is not None
    assert job["projectName"] == "Moon landing"
    assert job["prompt"] == "moon landing"
    assert job["model"] == "fast"
    assert job["resolution"] == "1080p"
    assert job["duration"] == 5
    assert job["fps"] == 24
    assert job["audio"] is False
    assert Path(str(job["video_path"])).exists()

    deleted = client.delete(f"/api/project-ingest/{job_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "ok"}
    assert client.get("/api/project-ingest").json() == {"jobs": []}


def test_delete_missing_ingest_job_returns_404(client) -> None:
    response = client.delete("/api/project-ingest/missing1")
    assert_http_error(response, status_code=404, code="HTTP_404", message="Ingest job not found")


def test_get_project_ingest_by_id_queued_then_complete(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 8
    pipeline.step_delay_s = 0.05

    r = client.post(
        "/api/generate",
        json={**_T2V_JSON, "prompt": "by id", "projectName": "Moon landing"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    job_id = r.json()["id"]

    assert pipeline.entered_inference.wait(timeout=5)

    queued = client.get(f"/api/project-ingest/{job_id}")
    assert queued.status_code == 200
    body = queued.json()
    assert body["id"] == job_id
    assert body["status"] == "queued"
    assert body["video_path"] == ""
    assert body["video_url"] is None
    assert body["prompt"] == "by id"

    deadline = time.time() + 15
    complete = None
    while time.time() < deadline:
        resp = client.get(f"/api/project-ingest/{job_id}")
        assert resp.status_code == 200
        if resp.json()["status"] == "complete":
            complete = resp.json()
            break
        time.sleep(0.05)
    assert complete is not None
    assert complete["video_path"]
    assert complete["video_url"] == f"/api/outputs/{Path(complete['video_path']).name}"
    assert Path(complete["video_path"]).exists()

    # Desktop gallery deletes after import; GET by id must still return the video URL.
    deleted = client.delete(f"/api/project-ingest/{job_id}")
    assert deleted.status_code == 200
    assert client.get("/api/project-ingest").json()["jobs"] == []
    after_import = client.get(f"/api/project-ingest/{job_id}")
    assert after_import.status_code == 200
    assert after_import.json()["status"] == "complete"
    assert after_import.json()["video_url"] == complete["video_url"]


def test_get_project_ingest_missing_returns_404(client) -> None:
    response = client.get("/api/project-ingest/missing1")
    assert_http_error(response, status_code=404, code="HTTP_404", message="Ingest job not found")


def _named_req(**overrides: object) -> GenerateVideoRequest:
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


def test_begin_lists_empty_video_path_complete_fills_drop_keeps_complete(test_state) -> None:
    ingest = test_state.project_ingest
    req = _named_req()
    ingest.begin_from_generate(req, "abc12345")
    jobs = ingest.list_jobs().jobs
    assert len(jobs) == 1
    assert jobs[0].id == "abc12345"
    assert jobs[0].projectName == "Moon landing"
    assert jobs[0].video_path == ""
    assert jobs[0].prompt == "moon landing"

    ingest.enqueue_from_generate(req, "/tmp/out.mp4", "abc12345")
    assert ingest.list_jobs().jobs[0].video_path == "/tmp/out.mp4"

    ingest.drop_if_incomplete("abc12345")
    assert len(ingest.list_jobs().jobs) == 1
    assert ingest.list_jobs().jobs[0].video_path == "/tmp/out.mp4"


def test_drop_if_incomplete_removes_running_job(test_state) -> None:
    ingest = test_state.project_ingest
    ingest.begin_from_generate(_named_req(), "deadbeef")
    ingest.drop_if_incomplete("deadbeef")
    assert ingest.list_jobs().jobs == []


def test_drop_orphaned_incomplete_keeps_live_and_complete(test_state) -> None:
    """Disk ingest survives process death; in-memory queue does not. Orphans must go."""
    ingest = test_state.project_ingest
    ingest.begin_from_generate(_named_req(prompt="stale"), "stale001")
    ingest.begin_from_generate(_named_req(prompt="live"), "live0001")
    ingest.begin_from_generate(_named_req(prompt="done"), "done0001")
    ingest.enqueue_from_generate(_named_req(prompt="done"), "/tmp/done.mp4", "done0001")

    ingest.drop_orphaned_incomplete({"live0001"})

    jobs = ingest.list_jobs().jobs
    assert {job.id for job in jobs} == {"live0001", "done0001"}
    assert next(job for job in jobs if job.id == "done0001").video_path == "/tmp/done.mp4"
    assert next(job for job in jobs if job.id == "live0001").video_path == ""


def test_list_project_ingest_drops_stale_incomplete_after_restart(client, test_state) -> None:
    # Simulate app close mid-queue: incomplete file on disk, empty in-memory queue.
    test_state.project_ingest.begin_from_generate(_named_req(prompt="orphaned"), "orphan01")
    assert len(test_state.project_ingest.list_jobs().jobs) == 1

    listed = client.get("/api/project-ingest")
    assert listed.status_code == 200
    assert listed.json() == {"jobs": []}


def test_list_jobs_orders_by_created_at_fifo(test_state) -> None:
    ingest = test_state.project_ingest
    ingest.begin_from_generate(_named_req(prompt="first"), "zzzz1111")
    time.sleep(0.02)
    ingest.begin_from_generate(_named_req(prompt="second"), "aaaa2222")
    time.sleep(0.02)
    ingest.begin_from_generate(_named_req(prompt="third"), "mmmm3333")
    jobs = ingest.list_jobs().jobs
    assert [job.prompt for job in jobs] == ["first", "second", "third"]
    assert [job.id for job in jobs] == ["zzzz1111", "aaaa2222", "mmmm3333"]


def test_complete_preserves_created_at_order(test_state) -> None:
    ingest = test_state.project_ingest
    ingest.begin_from_generate(_named_req(prompt="first"), "zzzz1111")
    time.sleep(0.02)
    ingest.begin_from_generate(_named_req(prompt="second"), "aaaa2222")
    first_created = ingest.list_jobs().jobs[0].createdAt
    ingest.enqueue_from_generate(_named_req(prompt="first"), "/tmp/first.mp4", "zzzz1111")
    jobs = ingest.list_jobs().jobs
    assert jobs[0].id == "zzzz1111"
    assert jobs[0].createdAt == first_created
    assert jobs[0].video_path == "/tmp/first.mp4"
    assert jobs[1].prompt == "second"


def test_begin_without_project_name_writes_nothing(test_state) -> None:
    ingest = test_state.project_ingest
    ingest.begin_from_generate(_named_req(projectName=None), "abc12345")
    ingest.begin_from_generate(_named_req(projectName="   "), "deadbeef")
    assert ingest.list_jobs().jobs == []


def test_in_flight_generate_lists_ingest_without_video_path(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 8
    pipeline.step_delay_s = 0.05

    r = client.post("/api/generate", json={**_T2V_JSON, "projectName": "Moon landing"})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    job_id = r.json()["id"]

    assert pipeline.entered_inference.wait(timeout=5)

    jobs = client.get("/api/project-ingest").json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == job_id
    assert jobs[0]["projectName"] == "Moon landing"
    assert jobs[0]["video_path"] == ""

    deadline = time.time() + 15
    while time.time() < deadline:
        listed = client.get("/api/project-ingest").json()["jobs"]
        if listed and listed[0]["video_path"]:
            break
        time.sleep(0.05)
    listed = client.get("/api/project-ingest").json()["jobs"]
    assert len(listed) == 1
    assert listed[0]["video_path"]


def test_cancelled_generate_drops_incomplete_ingest(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    fake_services.fast_video_pipeline.raise_on_generate = GenerationCancelledError()

    r = client.post("/api/generate", json={**_T2V_JSON, "projectName": "Moon landing"})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"

    deadline = time.time() + 10
    while time.time() < deadline:
        if client.get("/api/project-ingest").json()["jobs"] == []:
            break
        time.sleep(0.05)
    assert client.get("/api/project-ingest").json() == {"jobs": []}


def test_failed_generate_drops_incomplete_ingest(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    fake_services.fast_video_pipeline.raise_on_generate = RuntimeError("GPU OOM")

    r = client.post("/api/generate", json={**_T2V_JSON, "projectName": "Moon landing"})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"

    deadline = time.time() + 10
    while time.time() < deadline:
        if client.get("/api/project-ingest").json()["jobs"] == []:
            break
        time.sleep(0.05)
    assert client.get("/api/project-ingest").json() == {"jobs": []}


def test_second_generate_queues_instead_of_409(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 8
    pipeline.step_delay_s = 0.05

    r1 = client.post("/api/generate", json={**_T2V_JSON, "prompt": "first", "projectName": "Moon"})
    assert r1.status_code == 200
    assert r1.json()["status"] == "queued"
    assert pipeline.entered_inference.wait(timeout=5)

    r2 = client.post("/api/generate", json={**_T2V_JSON, "prompt": "second", "projectName": "Moon"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "queued"

    jobs: list[dict[str, object]] = []
    deadline = time.time() + 5
    while time.time() < deadline:
        jobs = client.get("/api/project-ingest").json()["jobs"]
        if len(jobs) == 2:
            break
        time.sleep(0.05)
    assert len(jobs) == 2
    assert {job["prompt"] for job in jobs} == {"first", "second"}
    assert all(job["video_path"] == "" for job in jobs)

    deadline = time.time() + 20
    while time.time() < deadline:
        listed = client.get("/api/project-ingest").json()["jobs"]
        if len(listed) == 2 and all(job["video_path"] for job in listed):
            break
        time.sleep(0.05)
    listed = client.get("/api/project-ingest").json()["jobs"]
    assert len(listed) == 2
    assert all(job["video_path"] for job in listed)


def test_cancel_queued_generate_does_not_stop_runner(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 12
    pipeline.step_delay_s = 0.05

    r1 = client.post("/api/generate", json={**_T2V_JSON, "prompt": "first", "projectName": "Moon"})
    assert r1.status_code == 200
    assert r1.json()["status"] == "queued"
    assert pipeline.entered_inference.wait(timeout=5)

    r2 = client.post("/api/generate", json={**_T2V_JSON, "prompt": "second", "projectName": "Moon"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "queued"
    queued_id = r2.json()["id"]

    jobs: list[dict[str, object]] = []
    deadline = time.time() + 5
    while time.time() < deadline:
        jobs = client.get("/api/project-ingest").json()["jobs"]
        if len(jobs) == 2:
            break
        time.sleep(0.05)
    assert len(jobs) == 2

    deleted = client.delete(f"/api/project-ingest/{queued_id}")
    assert deleted.status_code == 200

    deadline = time.time() + 15
    while time.time() < deadline:
        listed = client.get("/api/project-ingest").json()["jobs"]
        if len(listed) == 1 and listed[0]["video_path"]:
            break
        time.sleep(0.05)
    listed = client.get("/api/project-ingest").json()["jobs"]
    assert len(listed) == 1
    assert listed[0]["prompt"] == "first"
    assert listed[0]["video_path"]
    assert listed[0]["id"] == r1.json()["id"]


def test_many_project_name_generates_appear_in_ingest_immediately(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    """Regression: blocking POSTs exhausted browser connections so card 5 never appeared
    until an earlier job finished. Queued responses must free the connection immediately."""
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 20
    pipeline.step_delay_s = 0.05

    ids: list[str] = []
    for i in range(8):
        r = client.post(
            "/api/generate",
            json={**_T2V_JSON, "prompt": f"prompt {i}", "projectName": "Moon"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        ids.append(r.json()["id"])

    jobs = client.get("/api/project-ingest").json()["jobs"]
    assert len(jobs) == 8
    assert [job["id"] for job in jobs] == ids
    assert [job["prompt"] for job in jobs] == [f"prompt {i}" for i in range(8)]
    assert all(job["video_path"] == "" for job in jobs)

    # Stop the runner so the test does not wait for eight full fake generations.
    test_state.generation.cancel_generation()
    deadline = time.time() + 15
    while time.time() < deadline:
        if not client.get("/api/project-ingest").json()["jobs"]:
            break
        # Cancel only stops the running job; drop remaining queued via delete.
        for job in client.get("/api/project-ingest").json()["jobs"]:
            client.delete(f"/api/project-ingest/{job['id']}")
        time.sleep(0.05)


def test_generate_queue_full_returns_429(test_state, fake_services, create_fake_model_files) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 20
    pipeline.step_delay_s = 0.05
    test_state.video_generation._queue.max_size = 1

    first: dict[str, object] = {}
    threading.Thread(
        target=lambda: first.update(
            value=test_state.video_generation.generate(GenerateVideoRequest.model_validate(_T2V_JSON))
        )
    ).start()
    assert pipeline.entered_inference.wait(timeout=5)

    with pytest.raises(HTTPError) as exc_info:
        test_state.video_generation.generate(GenerateVideoRequest.model_validate(_T2V_JSON))
    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "VIDEO_GENERATE_QUEUE_FULL"

    client_cancel = test_state.generation.cancel_generation()
    assert client_cancel.status == "cancelling"
    deadline = time.time() + 10
    while time.time() < deadline and "value" not in first:
        time.sleep(0.05)
    assert first["value"].status == "cancelled"  # type: ignore[union-attr]


def test_delete_running_project_ingest_marks_sqlite_cancelled(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 40
    pipeline.step_delay_s = 0.05

    r = client.post(
        "/api/generate",
        json={**_T2V_JSON, "prompt": "running delete", "projectName": "Moon landing"},
    )
    assert r.status_code == 200
    job_id = r.json()["id"]
    assert pipeline.entered_inference.wait(timeout=5)
    assert pipeline.steps_completed < pipeline.inference_steps

    store = test_state.video_generation._queue_store
    # In-flight (not pending): cancel_queued is a no-op; DELETE must still cancel SQLite.
    assert store.get(job_id) is not None
    assert store.get(job_id).status in ("queued", "running")

    deleted = client.delete(f"/api/project-ingest/{job_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "ok"}

    row = store.get(job_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.status not in ("running", "queued")


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
    test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    pipeline = fake_services.fast_video_pipeline
    pipeline.inference_steps = 20
    pipeline.step_delay_s = 0.05
    test_state.video_generation._queue.max_size = 1

    first = test_state.video_generation.generate(
        GenerateVideoRequest.model_validate(
            {**_T2V_JSON, "prompt": "fill", "projectName": "Moon landing"}
        )
    )
    assert first.status == "queued"
    first_id = first.id
    assert pipeline.entered_inference.wait(timeout=5)

    with pytest.raises(HTTPError) as exc_info:
        test_state.video_generation.generate(
            GenerateVideoRequest.model_validate(
                {**_T2V_JSON, "prompt": "overflow", "projectName": "Moon landing"}
            )
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "VIDEO_GENERATE_QUEUE_FULL"

    store = test_state.video_generation._queue_store
    assert store.get(first_id) is not None
    assert all(job.request.prompt != "overflow" for job in store.list_incomplete())

    test_state.generation.cancel_generation()
    deadline = time.time() + 10
    while time.time() < deadline:
        if store.get(first_id) is not None and store.get(first_id).status in (
            "complete",
            "failed",
            "cancelled",
        ):
            break
        time.sleep(0.05)


def test_restart_resumes_incomplete_project_name_jobs(
    test_state, fake_services, create_fake_model_files
) -> None:
    """Simulate process death: durable SQLite rows + empty in-memory queue → new handler resumes."""
    from app_handler import ServiceBundle
    from state import build_initial_state, set_state_service_for_tests
    from state.app_settings import AppSettings
    from state.app_state_types import HfAuthenticated

    create_fake_model_files()
    _enable_local_text_encoding(test_state)

    id1, id2 = "resume001", "resume002"
    store = test_state.video_generation._queue_store
    store.insert_queued(
        id1,
        GenerateVideoRequest.model_validate(
            {**_T2V_JSON, "prompt": "first", "projectName": "Alpha"}
        ),
    )
    store.insert_queued(
        id2,
        GenerateVideoRequest.model_validate(
            {**_T2V_JSON, "prompt": "second", "projectName": "Beta"}
        ),
    )
    store.set_status(id1, "running")
    assert test_state.video_generation._queue.live_job_ids() == set()

    # Pretend process died: SQLite retained; rebuild AppHandler with same app_data_dir.
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
    resumed = build_initial_state(
        test_state.config, AppSettings().model_copy(deep=True), service_bundle=bundle
    )
    resumed.state.hf_auth_state = HfAuthenticated(access_token="fake-hf-token", expires_at=1e18)
    set_state_service_for_tests(resumed)
    _enable_local_text_encoding(resumed)

    assert {id1, id2} <= resumed.video_generation._queue.live_job_ids()

    deadline = time.time() + 30
    completed: set[str] = set()
    while time.time() < deadline:
        for jid in (id1, id2):
            row = resumed.video_generation._queue_store.get(jid)
            if row and row.status == "complete":
                completed.add(jid)
        if completed >= {id1, id2}:
            break
        time.sleep(0.05)
    assert completed >= {id1, id2}
