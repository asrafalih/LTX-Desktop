"""Default backend server port and bind-host helpers."""

PORT = 41954
DEFAULT_BIND_HOST = "127.0.0.1"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_VITE_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def resolve_bind_host(value: str | None) -> str:
    """Return the uvicorn bind host; empty/None stays localhost-only."""
    host = (value or "").strip()
    return host or DEFAULT_BIND_HOST


def advertise_bind_host(bind_host: str) -> str:
    """Host to print in the ready URL. Wildcards are not usable as a client URL."""
    if bind_host in {"0.0.0.0", "::", "[::]"}:
        return DEFAULT_BIND_HOST
    return bind_host


def is_loopback_bind(bind_host: str) -> bool:
    return bind_host.strip().lower() in _LOOPBACK_HOSTS


def assert_lan_bind_has_auth(bind_host: str, *tokens: str) -> None:
    """Refuse non-loopback bind when no auth secret is configured."""
    if is_loopback_bind(bind_host):
        return
    if any((token or "").strip() for token in tokens):
        return
    raise SystemExit(
        f"LTX_BIND_HOST={bind_host} requires a non-empty LTX_API_TOKEN "
        "(or LTX_AUTH_TOKEN). Refusing to listen on a non-loopback interface "
        "without authentication."
    )


def allowed_origins_for_bind(bind_host: str) -> list[str]:
    """CORS allowlist: Vite origins on loopback; any origin when LAN-exposed."""
    if is_loopback_bind(bind_host):
        return list(_VITE_ORIGINS)
    return ["*"]
