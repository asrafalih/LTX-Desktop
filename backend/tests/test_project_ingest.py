"""Tests for optional generate projectName ingest queue."""

from __future__ import annotations

import threading
from pathlib import Path

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
    video_path = r.json()["video_path"]
    assert Path(video_path).exists()

    listed = client.get("/api/project-ingest")
    assert listed.status_code == 200
    jobs = listed.json()["jobs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["projectName"] == "Moon landing"
    assert job["video_path"] == video_path
    assert job["prompt"] == "moon landing"
    assert job["model"] == "fast"
    assert job["resolution"] == "1080p"
    assert job["duration"] == 5
    assert job["fps"] == 24
    assert job["audio"] is False
    assert job["id"]

    deleted = client.delete(f"/api/project-ingest/{job['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "ok"}
    assert client.get("/api/project-ingest").json() == {"jobs": []}


def test_delete_missing_ingest_job_returns_404(client) -> None:
    response = client.delete("/api/project-ingest/missing1")
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

    req = GenerateVideoRequest.model_validate({**_T2V_JSON, "projectName": "Moon landing"})
    holder: dict[str, object] = {}

    def run() -> None:
        holder["result"] = test_state.video_generation.generate(req)

    thread = threading.Thread(target=run)
    thread.start()
    assert pipeline.entered_inference.wait(timeout=5)

    jobs = client.get("/api/project-ingest").json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["projectName"] == "Moon landing"
    assert jobs[0]["video_path"] == ""
    assert jobs[0]["id"]

    thread.join(timeout=10)
    assert not thread.is_alive()
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
    assert r.json()["status"] == "cancelled"
    assert client.get("/api/project-ingest").json() == {"jobs": []}


def test_failed_generate_drops_incomplete_ingest(
    client, test_state, fake_services, create_fake_model_files
) -> None:
    create_fake_model_files()
    _enable_local_text_encoding(test_state)
    fake_services.fast_video_pipeline.raise_on_generate = RuntimeError("GPU OOM")

    r = client.post("/api/generate", json={**_T2V_JSON, "projectName": "Moon landing"})
    assert r.status_code == 500
    assert client.get("/api/project-ingest").json() == {"jobs": []}
