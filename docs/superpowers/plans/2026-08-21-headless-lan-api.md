# Headless + LAN API Exposure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish env-first headless + LAN exposure of the existing FastAPI backend: public `/docs`, keyed `/api/*`, LAN bind guard, LAN CORS, `pnpm backend:serve`, and accurate docs.

**Architecture:** One FastAPI process (`ltx2_server.py` → `create_app`). Dual tokens already work (`LTX_AUTH_TOKEN` session + `LTX_API_TOKEN` stable). This plan closes remaining gaps: unauthenticated OpenAPI UI paths, refuse non-loopback bind without a token, widen CORS when LAN-bound, add a headless serve script, regenerate OpenAPI with Bearer, and fix stale port-8000 docs.

**Tech Stack:** FastAPI, Starlette TestClient, uvicorn, Node `pnpm` scripts, existing Electron env passthrough.

## Global Constraints

- No Settings UI for key / LAN (env-only).
- Full existing API for the API token (no LAN-only subset).
- Do not invent Docker, TLS, or rate limits.
- Default packaged/desktop behavior with no LAN env vars must stay loopback + random session token.
- Prefer documenting `LTX_API_TOKEN` as the stable LAN/script credential.
- Do not run headless and the desktop backend on the same port (document; no lock service).
- Reuse existing dual-token auth; do not re-implement Electron passthrough unless a regression appears.
- Tests: no `unittest.mock`; use TestClient + fakes.
- Backend commands via `pnpm backend:test -- …` or `cd backend && uv run pytest …`.

---

### File map

| File | Responsibility |
|------|----------------|
| `backend/runtime_config/port_constant.py` | Bind helpers + `is_loopback_bind` + `assert_lan_bind_has_auth` + `allowed_origins_for_bind` |
| `backend/app_factory.py` | Docs-path auth skip; CORS from caller; OpenAPI HTTP Bearer scheme |
| `backend/ltx2_server.py` | Resolve bind early; pass CORS origins; call LAN auth assert before listen |
| `backend/tests/test_bind_host.py` | Bind / LAN-guard / CORS-origin unit tests |
| `backend/tests/test_auth.py` | Docs path exemptions + `/api` still 401 |
| `backend/tests/test_http_error_responses.py` or new openapi assertion | Bearer security in OpenAPI |
| `scripts/backend-serve.mjs` | Headless serve: resolve app-data dir, spawn `ltx2_server.py` |
| `package.json` | `backend:serve` script |
| `README.md`, `AGENTS.md`, `CLAUDE.md` | Port `41954`, headless + LAN how-to |
| `docs/api-curl.md` | Point at `/docs` + `backend:serve` |
| `frontend/generated/backend-openapi.json` + `.ts` | Regenerated after Bearer scheme |

Already done (verify only; no task unless broken): Electron `LTX_API_TOKEN` / `LTX_BIND_HOST` / `LTX_PORT` passthrough; dual-token middleware accept.

---

### Task 1: LAN bind guard + CORS origin helper

**Files:**
- Modify: `backend/runtime_config/port_constant.py`
- Modify: `backend/tests/test_bind_host.py`

**Interfaces:**
- Consumes: existing `resolve_bind_host`, `advertise_bind_host`, `DEFAULT_BIND_HOST`
- Produces:
  - `is_loopback_bind(bind_host: str) -> bool`
  - `assert_lan_bind_has_auth(bind_host: str, *tokens: str) -> None` (raises `SystemExit` with message containing `LTX_API_TOKEN` when non-loopback and all tokens empty/whitespace)
  - `allowed_origins_for_bind(bind_host: str) -> list[str]` → Vite list on loopback, `["*"]` otherwise

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_bind_host.py`:

```python
import pytest

from runtime_config.port_constant import (
    allowed_origins_for_bind,
    assert_lan_bind_has_auth,
    is_loopback_bind,
)


def test_is_loopback_bind() -> None:
    assert is_loopback_bind("127.0.0.1") is True
    assert is_loopback_bind("localhost") is True
    assert is_loopback_bind("::1") is True
    assert is_loopback_bind("0.0.0.0") is False
    assert is_loopback_bind("192.168.1.10") is False


def test_assert_lan_bind_has_auth_allows_loopback_without_token() -> None:
    assert_lan_bind_has_auth("127.0.0.1", "", "")
    assert_lan_bind_has_auth("localhost")


def test_assert_lan_bind_has_auth_requires_token_for_wildcard() -> None:
    with pytest.raises(SystemExit, match="LTX_API_TOKEN"):
        assert_lan_bind_has_auth("0.0.0.0", "", "")


def test_assert_lan_bind_has_auth_accepts_api_or_session_token() -> None:
    assert_lan_bind_has_auth("0.0.0.0", "", "lan-secret")
    assert_lan_bind_has_auth("0.0.0.0", "session-secret", "")


def test_allowed_origins_for_bind() -> None:
    loopback = allowed_origins_for_bind("127.0.0.1")
    assert "http://localhost:5173" in loopback
    assert "http://127.0.0.1:5173" in loopback
    assert allowed_origins_for_bind("0.0.0.0") == ["*"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pnpm backend:test -- tests/test_bind_host.py -v
```

Expected: FAIL — `is_loopback_bind` / `assert_lan_bind_has_auth` / `allowed_origins_for_bind` not defined.

- [ ] **Step 3: Implement helpers in `port_constant.py`**

```python
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
```

Keep existing tests for `resolve_bind_host` / `advertise_bind_host` intact.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pnpm backend:test -- tests/test_bind_host.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/runtime_config/port_constant.py backend/tests/test_bind_host.py
git commit -m "$(cat <<'EOF'
feat: add LAN bind auth guard and CORS origin helper

EOF
)"
```

---

### Task 2: Unauthenticated docs paths + OpenAPI Bearer

**Files:**
- Modify: `backend/app_factory.py`
- Modify: `backend/tests/test_auth.py`
- Modify: `backend/tests/test_http_error_responses.py` (or add OpenAPI assertion there / in `test_auth.py`)

**Interfaces:**
- Consumes: existing `create_app(..., auth_token=, api_token=, allowed_origins=)`
- Produces: middleware skips auth for docs paths; OpenAPI schema includes `HTTPBearer` security scheme and global `security`

Unauthenticated exact paths:

```python
_UNAUTHENTICATED_PATHS = frozenset({
    "/docs",
    "/docs/",
    "/redoc",
    "/redoc/",
    "/openapi.json",
    "/docs/oauth2-redirect",
})
```

- [ ] **Step 1: Write the failing auth tests**

Append to `backend/tests/test_auth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pnpm backend:test -- tests/test_auth.py -v
```

Expected: FAIL — `/docs` returns 401; OpenAPI missing `HTTPBearer`.

- [ ] **Step 3: Implement in `app_factory.py`**

1. Add near top (after imports / constants):

```python
from fastapi.openapi.utils import get_openapi

_UNAUTHENTICATED_PATHS = frozenset({
    "/docs",
    "/docs/",
    "/redoc",
    "/redoc/",
    "/openapi.json",
    "/docs/oauth2-redirect",
})
```

2. In `_auth_middleware`, after OPTIONS / HF callback checks, before token matching:

```python
        if request.url.path in _UNAUTHENTICATED_PATHS:
            return await call_next(request)
```

3. Before `return app`, attach custom OpenAPI (only when at least one secret is configured — optional; always declaring Bearer is fine and simpler):

```python
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["HTTPBearer"] = {
            "type": "http",
            "scheme": "bearer",
        }
        schema["security"] = [{"HTTPBearer": []}]
        # Keep error response models already attached via responses=
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
```

If `get_openapi` drops `responses` from `DEFAULT_ERROR_RESPONSES`, prefer mutating the default `app.openapi()` result instead:

```python
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=getattr(app, "version", None) or "0.1.0",
            routes=app.routes,
        )
        # ... same securitySchemes injection ...
        app.openapi_schema = schema
        return schema
```

Preserve existing `test_app_openapi_registers_shared_http_error_response` — if it fails, merge security into the schema returned by FastAPI’s default generation without stripping `components.schemas.HTTPErrorResponse`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pnpm backend:test -- tests/test_auth.py tests/test_http_error_responses.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app_factory.py backend/tests/test_auth.py
git commit -m "$(cat <<'EOF'
feat: allow unauthenticated OpenAPI docs with Bearer scheme

EOF
)"
```

---

### Task 3: Wire bind guard + LAN CORS in `ltx2_server.py`

**Files:**
- Modify: `backend/ltx2_server.py`
- Test: covered by Task 1 unit tests + optional thin test if you extract the wiring; no need to boot uvicorn in CI

**Interfaces:**
- Consumes: `resolve_bind_host`, `advertise_bind_host`, `assert_lan_bind_has_auth`, `allowed_origins_for_bind`
- Produces: `create_app(..., allowed_origins=allowed_origins_for_bind(bind_host))`; `__main__` calls `assert_lan_bind_has_auth` before `uvicorn` serve

- [ ] **Step 1: Resolve bind host before `create_app`**

Near where `runtime_config` / tokens are read, replace hardcoded CORS with:

```python
from runtime_config.port_constant import (
    PORT,
    advertise_bind_host,
    allowed_origins_for_bind,
    assert_lan_bind_has_auth,
    resolve_bind_host,
)

bind_host = resolve_bind_host(os.environ.get("LTX_BIND_HOST"))

auth_token = os.environ.get("LTX_AUTH_TOKEN", "")
api_token = os.environ.get("LTX_API_TOKEN", "")
admin_token = os.environ.get("LTX_ADMIN_TOKEN", "")

app = create_app(
    handler=handler,
    allowed_origins=allowed_origins_for_bind(bind_host),
    auth_token=auth_token,
    api_token=api_token,
    admin_token=admin_token,
)
```

Remove unused `DEFAULT_ALLOWED_ORIGINS` import from `ltx2_server.py` if it is only used here.

- [ ] **Step 2: Guard before listen in `__main__`**

Inside `if __name__ == "__main__":`, after computing `bind_host` / tokens (reuse module-level `bind_host`, `auth_token`, `api_token` — do not re-resolve differently), before `uvicorn.Config(...)`:

```python
    assert_lan_bind_has_auth(bind_host, auth_token, api_token)
```

Keep ready message using `advertise_bind_host(bind_host)`.

- [ ] **Step 3: Add CORS integration test**

Append to `backend/tests/test_auth.py` (or `test_health.py`):

```python
def test_lan_cors_allows_any_origin(test_state):
    from runtime_config.port_constant import allowed_origins_for_bind

    app = create_app(
        handler=test_state,
        auth_token="test-secret",
        allowed_origins=allowed_origins_for_bind("0.0.0.0"),
    )
    with TestClient(app) as client:
        r = client.get(
            "/health",
            headers={
                "Authorization": "Bearer test-secret",
                "Origin": "http://192.168.1.50:3000",
            },
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") in {"*", "http://192.168.1.50:3000"}
```

- [ ] **Step 4: Run tests**

```bash
pnpm backend:test -- tests/test_auth.py tests/test_bind_host.py tests/test_health.py -v
```

Expected: PASS (loopback CORS test in `test_health` still expects Vite origin).

- [ ] **Step 5: Commit**

```bash
git add backend/ltx2_server.py backend/tests/test_auth.py
git commit -m "$(cat <<'EOF'
feat: wire LAN bind auth guard and CORS into backend server

EOF
)"
```

---

### Task 4: `pnpm backend:serve` headless entrypoint

**Files:**
- Create: `scripts/backend-serve.mjs`
- Modify: `package.json`

**Interfaces:**
- Consumes: same app-data resolution as `scripts/perf-dev.mjs` (`LTXDesktop` folder)
- Produces: spawns `ltx2_server.py` with `LTX_APP_DATA_DIR` set if unset; forwards `LTX_API_TOKEN`, `LTX_AUTH_TOKEN`, `LTX_BIND_HOST`, `LTX_PORT`; does **not** invent a random token

- [ ] **Step 1: Add `scripts/backend-serve.mjs`**

Mirror the `appDataDir()` helper from `scripts/perf-dev.mjs`. Script outline:

```js
#!/usr/bin/env node
/**
 * backend:serve — run the FastAPI backend headless (no Electron).
 *
 * Reuses the desktop app data dir (models/settings) unless LTX_APP_DATA_DIR is set.
 * Set LTX_API_TOKEN for scripts/LAN. Set LTX_BIND_HOST=0.0.0.0 for LAN (requires a token).
 *
 *   LTX_API_TOKEN=… pnpm backend:serve
 *   LTX_API_TOKEN=… LTX_BIND_HOST=0.0.0.0 pnpm backend:serve
 */
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const BACKEND_DIR = path.join(ROOT, 'backend')
const APP_FOLDER_NAME = 'LTXDesktop'
const PORT = process.env.LTX_PORT || '41954'
const BIND = process.env.LTX_BIND_HOST || '127.0.0.1'

function appDataDir() {
  if (process.env.LTX_APP_DATA_DIR) return process.env.LTX_APP_DATA_DIR
  if (process.platform === 'win32') {
    const base = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')
    return path.join(base, APP_FOLDER_NAME)
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', APP_FOLDER_NAME)
  }
  const xdg = process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share')
  return path.join(xdg, APP_FOLDER_NAME)
}

const HAS_UV = spawnSync('uv', ['--version'], { stdio: 'ignore' }).status === 0
const appData = appDataDir()
const token = process.env.LTX_API_TOKEN || process.env.LTX_AUTH_TOKEN || ''

console.log('\nbackend:serve — headless FastAPI')
console.log(`  app data : ${appData}`)
console.log(`  listen   : http://${BIND}:${PORT}`)
console.log(`  docs     : http://127.0.0.1:${PORT}/docs  (also via LAN IP when bind is 0.0.0.0)`)
console.log(`  token    : ${token ? 'set (LTX_API_TOKEN / LTX_AUTH_TOKEN)' : 'NOT SET — /api requires a token when configured; LAN bind will refuse to start'}`)
console.log('  Do not run this while the desktop app backend is already on the same port.')
console.log('  Ctrl+C to stop.\n')

const args = HAS_UV ? ['run', 'python', 'ltx2_server.py'] : ['ltx2_server.py']
const cmd = HAS_UV ? 'uv' : 'python'
const child = spawn(cmd, args, {
  cwd: BACKEND_DIR,
  env: {
    ...process.env,
    LTX_APP_DATA_DIR: appData,
    PYTHONUNBUFFERED: '1',
    LTX_DEV_MODE: process.env.LTX_DEV_MODE || '1',
    PYTORCH_ENABLE_MPS_FALLBACK: '1',
  },
  stdio: 'inherit',
})

child.on('exit', (code) => process.exit(code ?? 1))
process.on('SIGINT', () => child.kill('SIGINT'))
process.on('SIGTERM', () => child.kill('SIGTERM'))
```

- [ ] **Step 2: Add package script**

In `package.json` scripts:

```json
"backend:serve": "node scripts/backend-serve.mjs"
```

- [ ] **Step 3: Smoke-check script starts (optional local)**

With the desktop app **stopped** (so port is free):

```bash
LTX_API_TOKEN=dev-test-token pnpm backend:serve
```

Expected: logs bind host, prints `Server running on http://127.0.0.1:41954`, and:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:41954/docs
# 200
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:41954/health
# 401
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer dev-test-token' http://127.0.0.1:41954/health
# 200
```

Stop with Ctrl+C. If port busy because `pnpm dev` is running, skip live smoke and rely on unit tests.

- [ ] **Step 4: Commit**

```bash
git add scripts/backend-serve.mjs package.json
git commit -m "$(cat <<'EOF'
feat: add pnpm backend:serve for headless API

EOF
)"
```

---

### Task 5: Docs + OpenAPI regenerate

**Files:**
- Modify: `README.md` (architecture port + Development section)
- Modify: `AGENTS.md` and `CLAUDE.md` (port line)
- Modify: `docs/api-curl.md` (intro: how to start + `/docs`)
- Regenerate: `frontend/generated/backend-openapi.json`, `frontend/generated/backend-openapi.ts`

- [ ] **Step 1: Fix port references**

In `README.md` architecture bullets/mermaid, replace `localhost:8000` with `localhost:41954` (or `127.0.0.1:41954`).

In `AGENTS.md` / `CLAUDE.md`:

```markdown
- **Backend** (`backend/`): Python FastAPI server (port 41954) handling ML model orchestration and generation
```

- [ ] **Step 2: Document headless + LAN in README Development section**

After the `pnpm perf:dev` block, add something like:

```markdown
### Headless API (local / LAN)

Run the backend without Electron (reuses the app’s models/settings directory):

```bash
LTX_API_TOKEN=your-stable-token pnpm backend:serve
```

- Docs UI: `http://127.0.0.1:41954/docs` (no token required to view; Authorize with the Bearer token to try endpoints)
- API calls need `Authorization: Bearer $LTX_API_TOKEN`
- LAN: `LTX_BIND_HOST=0.0.0.0 LTX_API_TOKEN=… pnpm backend:serve` then use `http://<machine-lan-ip>:41954`
- Do not run `backend:serve` while the desktop app is already using the same port
- Curl cookbook: [`docs/api-curl.md`](docs/api-curl.md)

With the desktop app running, the same env vars work if set before launch (`LTX_API_TOKEN`, `LTX_BIND_HOST`, `LTX_PORT`); Electron still generates a session token for the UI.
```

Also add `docs/api-curl.md` to the Docs list at the bottom of README.

- [ ] **Step 3: Update `docs/api-curl.md` intro**

At the top, mention:

```markdown
Start headless with `LTX_API_TOKEN=… pnpm backend:serve`, or set `LTX_API_TOKEN` /
`LTX_BIND_HOST` when launching the desktop app. Interactive docs: `$LTX_HOST/docs`
(Swagger loads without a token; click Authorize and paste the Bearer token).
```

Keep existing curl examples using `LTX_API_TOKEN`.

- [ ] **Step 4: Regenerate OpenAPI**

```bash
pnpm openapi:generate
pnpm openapi:check
```

Expected: schema includes `components.securitySchemes.HTTPBearer` and top-level `security`; check exits 0 (or leaves intentional diffs that you commit).

- [ ] **Step 5: Commit**

```bash
git add README.md AGENTS.md CLAUDE.md docs/api-curl.md \
  frontend/generated/backend-openapi.json frontend/generated/backend-openapi.ts
git commit -m "$(cat <<'EOF'
docs: document headless/LAN API and refresh OpenAPI Bearer

EOF
)"
```

---

### Task 6: Final verification

- [ ] **Step 1: Run focused backend tests**

```bash
pnpm backend:test -- tests/test_auth.py tests/test_bind_host.py tests/test_health.py tests/test_http_error_responses.py -v
```

Expected: all PASS.

- [ ] **Step 2: Typecheck if Python surface changed signatures used elsewhere**

```bash
pnpm typecheck:py
```

Expected: PASS.

- [ ] **Step 3: Confirm Electron passthrough still present**

In `electron/python-backend.ts`, verify these still exist (no change needed if present):

- `process.env.LTX_BIND_HOST` forwarded
- `LTX_API_TOKEN` from `process.env.LTX_API_TOKEN || process.env.LTX_AUTH_TOKEN`
- Session `LTX_AUTH_TOKEN` still randomly generated for the UI

- [ ] **Step 4: Commit any leftover fixes** (only if Step 1–3 required code changes)

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| Stable `LTX_API_TOKEN` + session dual accept | Already present; verified in Task 6 |
| `LTX_BIND_HOST` default loopback / `0.0.0.0` LAN | Already present + Task 1/3 |
| Refuse non-loopback without token | Task 1 + 3 |
| `/docs` `/redoc` `/openapi.json` public | Task 2 |
| OpenAPI HTTP Bearer | Task 2 + 5 |
| LAN CORS `["*"]` | Task 1 + 3 |
| `pnpm backend:serve` + shared app data | Task 4 |
| Docs: port 41954, headless/LAN, don’t double-bind | Task 5 |
| Tests for exemptions, guard, CORS, dual-token | Tasks 1–3, 6 |
| No Settings UI / no Docker / full API | Global constraints |

## Placeholder / consistency self-review

- No TBD/TODO steps; concrete code and commands included.
- Helper names (`assert_lan_bind_has_auth`, `allowed_origins_for_bind`, `is_loopback_bind`) consistent across tasks.
- `LTX_API_TOKEN` is the documented LAN credential; session token remains `LTX_AUTH_TOKEN`.
