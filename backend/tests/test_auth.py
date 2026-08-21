"""Tests for shared-secret authentication middleware."""

from __future__ import annotations

import base64

from starlette.testclient import TestClient

from app_factory import create_app
from tests.http_error_assertions import assert_http_error


def test_request_without_token_returns_401(test_state):
    app = create_app(handler=test_state, auth_token="test-secret")
    with TestClient(app) as client:
        response = client.get("/health")
        assert_http_error(response, status_code=401, code="HTTP_401", message="Unauthorized")


def test_request_with_correct_bearer_token(test_state):
    app = create_app(handler=test_state, auth_token="test-secret")
    with TestClient(app) as client:
        response = client.get("/health", headers={"Authorization": "Bearer test-secret"})
        assert response.status_code == 200


def test_request_with_correct_basic_auth(test_state):
    app = create_app(handler=test_state, auth_token="test-secret")
    credentials = base64.b64encode(b":test-secret").decode()
    with TestClient(app) as client:
        response = client.get("/health", headers={"Authorization": f"Basic {credentials}"})
        assert response.status_code == 200


def test_request_with_wrong_token_returns_401(test_state):
    app = create_app(handler=test_state, auth_token="test-secret")
    with TestClient(app) as client:
        response = client.get("/health", headers={"Authorization": "Bearer wrong-token"})
        assert_http_error(response, status_code=401, code="HTTP_401", message="Unauthorized")


def test_health_without_token_returns_401(test_state):
    """Health endpoint is NOT exempt from auth."""
    app = create_app(handler=test_state, auth_token="test-secret")
    with TestClient(app) as client:
        response = client.get("/health")
        assert_http_error(response, status_code=401, code="HTTP_401", message="Unauthorized")


def test_no_auth_token_disables_middleware(test_state):
    """When auth_token is empty string, auth is disabled (dev/test mode)."""
    app = create_app(handler=test_state, auth_token="")
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200


def test_websocket_with_token_query_param(test_state):
    app = create_app(handler=test_state, auth_token="test-secret")
    with TestClient(app) as client:
        # WebSocket upgrade without token should fail with 401
        response = client.get(
            "/ws/download/test",
            headers={"upgrade": "websocket", "connection": "upgrade"},
        )
        assert_http_error(response, status_code=401, code="HTTP_401", message="Unauthorized")

        # WebSocket upgrade with correct token query param
        response = client.get(
            "/ws/download/test?token=test-secret",
            headers={"upgrade": "websocket", "connection": "upgrade"},
        )
        # The route may not exist, but auth should pass (not 401)
        assert response.status_code != 401


def test_session_or_api_token_both_accepted(test_state):
    app = create_app(handler=test_state, auth_token="session-secret", api_token="lan-secret")
    with TestClient(app) as client:
        session = client.get("/health", headers={"Authorization": "Bearer session-secret"})
        assert session.status_code == 200
        lan = client.get("/health", headers={"Authorization": "Bearer lan-secret"})
        assert lan.status_code == 200
        wrong = client.get("/health", headers={"Authorization": "Bearer other-secret"})
        assert_http_error(wrong, status_code=401, code="HTTP_401", message="Unauthorized")


def test_api_token_only_accepted(test_state):
    app = create_app(handler=test_state, auth_token="", api_token="lan-secret")
    with TestClient(app) as client:
        ok = client.get("/health", headers={"Authorization": "Bearer lan-secret"})
        assert ok.status_code == 200
        missing = client.get("/health")
        assert_http_error(missing, status_code=401, code="HTTP_401", message="Unauthorized")


def test_docs_and_openapi_are_public_when_auth_configured(test_state):
    app = create_app(handler=test_state, auth_token="test-secret")
    with TestClient(app) as client:
        docs = client.get("/docs")
        assert docs.status_code == 200
        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        redoc = client.get("/redoc")
        assert redoc.status_code == 200


def test_api_still_requires_token_when_docs_are_public(test_state):
    app = create_app(handler=test_state, auth_token="test-secret")
    with TestClient(app) as client:
        response = client.get("/health")
        assert_http_error(response, status_code=401, code="HTTP_401", message="Unauthorized")
        ok = client.get("/health", headers={"Authorization": "Bearer test-secret"})
        assert ok.status_code == 200


def test_openapi_declares_http_bearer(test_state):
    app = create_app(handler=test_state, auth_token="test-secret")
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["HTTPBearer"]["type"] == "http"
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    assert {"HTTPBearer": []} in schema.get("security", [])
