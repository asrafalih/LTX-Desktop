"""Tests for GET /api/outputs/{filename} (LAN media download)."""

from __future__ import annotations

from starlette.testclient import TestClient

from app_factory import create_app
from tests.http_error_assertions import assert_http_error


def test_download_output_returns_file_bytes(client, test_state) -> None:
    payload = b"fake-mp4-bytes"
    path = test_state.config.outputs_dir / "ltx2_video_test.mp4"
    path.write_bytes(payload)

    response = client.get("/api/outputs/ltx2_video_test.mp4")

    assert response.status_code == 200
    assert response.content == payload
    assert "video/mp4" in response.headers.get("content-type", "")


def test_download_output_requires_auth(test_state) -> None:
    path = test_state.config.outputs_dir / "ltx2_video_auth.mp4"
    path.write_bytes(b"secret")
    app = create_app(handler=test_state, auth_token="test-secret")
    with TestClient(app) as client:
        unauth = client.get("/api/outputs/ltx2_video_auth.mp4")
        assert_http_error(unauth, status_code=401, code="HTTP_401", message="Unauthorized")

        ok = client.get(
            "/api/outputs/ltx2_video_auth.mp4",
            headers={"Authorization": "Bearer test-secret"},
        )
        assert ok.status_code == 200
        assert ok.content == b"secret"


def test_download_output_missing_file_returns_404(client) -> None:
    response = client.get("/api/outputs/missing_clip.mp4")
    assert_http_error(response, status_code=404, code="HTTP_404", message="Output file not found")


def test_download_output_rejects_path_traversal(client, test_state, tmp_path) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not-an-output")

    encoded = client.get("/api/outputs/..%2Foutside.mp4")
    assert encoded.status_code in {400, 404}

    dotted = client.get("/api/outputs/..")
    assert dotted.status_code in {400, 404}


def test_download_output_rejects_disallowed_extension(client, test_state) -> None:
    path = test_state.config.outputs_dir / "notes.txt"
    path.write_text("nope")

    response = client.get("/api/outputs/notes.txt")
    assert_http_error(response, status_code=400, code="HTTP_400", message="Unsupported output file type")
