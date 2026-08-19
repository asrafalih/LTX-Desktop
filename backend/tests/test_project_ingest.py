"""Tests for optional generate projectName ingest queue."""

from __future__ import annotations

from pathlib import Path

from tests.http_error_assertions import assert_http_error
from tests.test_generation import _enable_local_text_encoding


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
