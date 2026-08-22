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
