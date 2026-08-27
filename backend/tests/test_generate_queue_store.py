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
