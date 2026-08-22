"""Tests for POST/GET /api/uploads and generate media-ref resolution."""

from __future__ import annotations

import time
from pathlib import Path

from starlette.testclient import TestClient

from app_factory import create_app
from server_utils.media_validation import resolve_media_ref
from tests.http_error_assertions import assert_http_error
from _routes._errors import HTTPError


def test_upload_image_returns_api_url(client, make_test_image) -> None:
    response = client.post(
        "/api/uploads",
        files={"file": ("start.png", make_test_image().getvalue(), "image/png")},
        data={"kind": "image"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["url"].startswith("/api/uploads/")
    assert data["url"].endswith(".png")


def test_upload_requires_auth(test_state, make_test_image) -> None:
    app = create_app(handler=test_state, auth_token="test-secret")
    with TestClient(app) as client:
        unauth = client.post(
            "/api/uploads",
            files={"file": ("start.png", make_test_image().getvalue(), "image/png")},
            data={"kind": "image"},
        )
        assert_http_error(unauth, status_code=401, code="HTTP_401", message="Unauthorized")

        ok = client.post(
            "/api/uploads",
            files={"file": ("start.png", make_test_image().getvalue(), "image/png")},
            data={"kind": "image"},
            headers={"Authorization": "Bearer test-secret"},
        )
        assert ok.status_code == 200


def test_get_upload_returns_bytes(client, make_test_image) -> None:
    payload = make_test_image().getvalue()
    uploaded = client.post(
        "/api/uploads",
        files={"file": ("start.png", payload, "image/png")},
        data={"kind": "image"},
    )
    url = uploaded.json()["url"]
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == payload


def test_get_upload_rejects_path_traversal(client) -> None:
    encoded = client.get("/api/uploads/..%2Foutside.png")
    assert encoded.status_code in {400, 404}
    dotted = client.get("/api/uploads/..")
    assert dotted.status_code in {400, 404}


def test_resolve_media_ref_maps_upload_url(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    stored = uploads / "abc123.png"
    stored.write_bytes(b"x")
    resolved = resolve_media_ref("/api/uploads/abc123.png", uploads_dir=uploads)
    assert Path(resolved) == stored.resolve()


def test_resolve_media_ref_passes_absolute_path(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    abs_path = str((tmp_path / "photo.jpg").resolve())
    assert resolve_media_ref(abs_path, uploads_dir=uploads) == abs_path


def test_resolve_media_ref_rejects_other_urls(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    for bad in ("http://example.com/a.png", "/api/outputs/clip.mp4", "https://x/y"):
        try:
            resolve_media_ref(bad, uploads_dir=uploads)
            raise AssertionError(f"expected HTTPError for {bad}")
        except HTTPError as exc:
            assert exc.status_code == 400


def test_generate_accepts_upload_url_as_image_path(
    client, test_state, fake_services, make_test_image
) -> None:
    test_state.config.local_generations_mode = "unsupported"
    test_state.state.app_settings.ltx_api_key = "api-key"

    uploaded = client.post(
        "/api/uploads",
        files={"file": ("start.png", make_test_image().getvalue(), "image/png")},
        data={"kind": "image"},
    )
    assert uploaded.status_code == 200
    image_url = uploaded.json()["url"]
    stored = test_state.config.app_data_dir / "uploads" / image_url.rsplit("/", 1)[-1]

    response = client.post(
        "/api/generate",
        json={
            "prompt": "waves",
            "model": "fast",
            "duration": 6,
            "fps": 25,
            "resolution": "1080p",
            "cameraMotion": "none",
            "imagePath": image_url,
            "audio": False,
            "aspectRatio": "16:9",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert len(fake_services.ltx_api_client.upload_file_calls) == 1
    assert fake_services.ltx_api_client.upload_file_calls[0]["file_path"] == str(stored)


def test_sweep_removes_uploads_older_than_24h(client, test_state, make_test_image) -> None:
    uploaded = client.post(
        "/api/uploads",
        files={"file": ("old.png", make_test_image().getvalue(), "image/png")},
        data={"kind": "image"},
    )
    url = uploaded.json()["url"]
    filename = url.rsplit("/", 1)[-1]
    path = test_state.config.app_data_dir / "uploads" / filename
    assert path.is_file()

    old = time.time() - (25 * 60 * 60)
    # Touch mtime into the past
    import os

    os.utime(path, (old, old))

    # Trigger sweep via another upload
    client.post(
        "/api/uploads",
        files={"file": ("new.png", make_test_image().getvalue(), "image/png")},
        data={"kind": "image"},
    )
    assert not path.exists()
