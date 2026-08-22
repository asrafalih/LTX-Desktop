"""FastAPI app factory decoupled from runtime bootstrap side effects."""

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response as StarletteResponse

from _routes._errors import HTTPError, build_http_error_response
from _routes.generation import router as generation_router
from _routes.hf_auth import router as hf_auth_router
from _routes.health import router as health_router
from _routes.ic_lora import router as ic_lora_router
from _routes.lora_catalog import router as lora_catalog_router
from _routes.image_gen import router as image_gen_router
from _routes.prompt_enhancement import router as prompt_enhancement_router
from _routes.models import router as models_router
from _routes.outputs import router as outputs_router
from _routes.uploads import router as uploads_router
from _routes.project_ingest import router as project_ingest_router
from _routes.suggest_gap_prompt import router as suggest_gap_prompt_router
from _routes.retake import router as retake_router
from _routes.extend import router as extend_router
from _routes.runtime_policy import router as runtime_policy_router
from _routes.settings import router as settings_router
from api_types import HTTPErrorResponse
from logging_policy import log_http_error, log_unhandled_exception
from state import init_state_service

if TYPE_CHECKING:
    from app_handler import AppHandler

DEFAULT_ALLOWED_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
]

DEFAULT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "4XX": {
        "model": HTTPErrorResponse,
        "description": "Client Error",
    },
    "5XX": {
        "model": HTTPErrorResponse,
        "description": "Server Error",
    },
}

_UNAUTHENTICATED_PATHS = frozenset({
    "/docs",
    "/docs/",
    "/redoc",
    "/redoc/",
    "/openapi.json",
    "/docs/oauth2-redirect",
})


def _looks_like_json(body: bytes) -> bool:
    stripped = body.lstrip()
    return stripped.startswith(b"{") or stripped.startswith(b"[")


def _strict_json_bytes(body: bytes) -> bytes | None:
    """Re-encode JSON that contains raw control characters inside strings (curl $'...\\n...')."""
    try:
        json.loads(body)
        return body
    except json.JSONDecodeError:
        pass
    try:
        parsed: object = json.loads(body, strict=False)
    except json.JSONDecodeError:
        return None
    return json.dumps(parsed, ensure_ascii=False).encode("utf-8")


def _replace_header(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> list[tuple[bytes, bytes]]:
    lowered = name.lower()
    replaced = [(key, val) for key, val in headers if key.lower() != lowered]
    replaced.append((name, value))
    return replaced


def create_app(
    *,
    handler: "AppHandler",
    allowed_origins: list[str] | None = None,
    title: str = "LTX-2 Video Generation Server",
    auth_token: str = "",
    api_token: str = "",
    admin_token: str = "",
) -> FastAPI:
    """Create a configured FastAPI app bound to the provided handler."""
    init_state_service(handler)

    app = FastAPI(title=title, responses=DEFAULT_ERROR_RESPONSES)
    app.state.admin_token = admin_token  # type: ignore[attr-defined]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins or DEFAULT_ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    secrets = tuple(token for token in (auth_token, api_token) if token)

    @app.middleware("http")
    async def _auth_middleware(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        call_next: Callable[[Request], Awaitable[StarletteResponse]],
    ) -> StarletteResponse:
        if not secrets:
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.url.path == "/api/auth/huggingface/callback":
            return await call_next(request)
        if request.url.path in _UNAUTHENTICATED_PATHS:
            return await call_next(request)
        def _token_matches(candidate: str) -> bool:
            matched = False
            for secret in secrets:
                try:
                    if hmac.compare_digest(candidate, secret):
                        matched = True
                except (TypeError, ValueError):
                    pass
            return matched

        # WebSocket: check query param
        if request.headers.get("upgrade", "").lower() == "websocket":
            if _token_matches(request.query_params.get("token", "")):
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content=build_http_error_response(401, "Unauthorized").model_dump(),
            )
        # HTTP: Bearer or Basic auth
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer ") and _token_matches(auth_header[7:]):
            return await call_next(request)
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                _, _, password = decoded.partition(":")
                if _token_matches(password):
                    return await call_next(request)
            except Exception:
                pass
        return JSONResponse(
            status_code=401,
            content=build_http_error_response(401, "Unauthorized").model_dump(),
        )

    @app.middleware("http")
    async def _coerce_json_content_type(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        call_next: Callable[[Request], Awaitable[StarletteResponse]],
    ) -> StarletteResponse:
        # curl --data-raw defaults to application/x-www-form-urlencoded; if the body is JSON,
        # treat it as JSON so Pydantic receives a dict instead of raw bytes.
        if request.method in {"POST", "PUT", "PATCH"}:
            body = await request.body()
            if _looks_like_json(body):
                headers = list(request.scope["headers"])
                content_type = request.headers.get("content-type", "")
                if "application/json" not in content_type.lower():
                    headers = _replace_header(headers, b"content-type", b"application/json")
                normalized = _strict_json_bytes(body)
                if normalized is not None and normalized != body:
                    # Starlette caches the first body() read; replace it with strict JSON.
                    setattr(request, "_body", normalized)
                    headers = _replace_header(
                        headers, b"content-length", str(len(normalized)).encode("ascii")
                    )
                request.scope["headers"] = headers
        return await call_next(request)

    async def _route_http_error_handler(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPError):
            log_http_error(request, exc)
            return JSONResponse(status_code=exc.status_code, content=exc.response.model_dump())
        return JSONResponse(
            status_code=500,
            content=build_http_error_response(500, str(exc)).model_dump(),
        )

    async def _starlette_http_error_handler(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, StarletteHTTPException):
            return JSONResponse(
                status_code=exc.status_code,
                content=build_http_error_response(exc.status_code, exc.detail).model_dump(),
            )
        return JSONResponse(
            status_code=500,
            content=build_http_error_response(500, str(exc)).model_dump(),
        )

    async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, RequestValidationError):
            return JSONResponse(
                status_code=422,
                content=build_http_error_response(422, str(exc)).model_dump(),
            )
        return JSONResponse(
            status_code=422,
            content=build_http_error_response(422, str(exc)).model_dump(),
        )

    async def _route_generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log_unhandled_exception(request, exc)
        return JSONResponse(
            status_code=500,
            content=build_http_error_response(500, str(exc)).model_dump(),
        )

    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(HTTPError, _route_http_error_handler)
    app.add_exception_handler(StarletteHTTPException, _starlette_http_error_handler)
    app.add_exception_handler(Exception, _route_generic_error_handler)

    app.include_router(health_router)
    app.include_router(generation_router)
    app.include_router(outputs_router)
    app.include_router(uploads_router)
    app.include_router(project_ingest_router)
    app.include_router(models_router)
    app.include_router(settings_router)
    app.include_router(image_gen_router)
    app.include_router(suggest_gap_prompt_router)
    app.include_router(retake_router)
    app.include_router(extend_router)
    app.include_router(ic_lora_router)
    app.include_router(lora_catalog_router)
    app.include_router(prompt_enhancement_router)
    app.include_router(runtime_policy_router)
    app.include_router(hf_auth_router)

    _default_openapi = app.openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = _default_openapi()
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["HTTPBearer"] = {
            "type": "http",
            "scheme": "bearer",
        }
        schema["security"] = [{"HTTPBearer": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    return app
