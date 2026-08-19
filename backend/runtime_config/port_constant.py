"""Default backend server port and bind-host helpers."""

PORT = 41954
DEFAULT_BIND_HOST = "127.0.0.1"


def resolve_bind_host(value: str | None) -> str:
    """Return the uvicorn bind host; empty/None stays localhost-only."""
    host = (value or "").strip()
    return host or DEFAULT_BIND_HOST


def advertise_bind_host(bind_host: str) -> str:
    """Host to print in the ready URL. Wildcards are not usable as a client URL."""
    if bind_host in {"0.0.0.0", "::", "[::]"}:
        return DEFAULT_BIND_HOST
    return bind_host
