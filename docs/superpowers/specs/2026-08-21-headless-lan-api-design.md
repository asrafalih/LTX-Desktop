# Headless + LAN API exposure — design

**Date:** 2026-08-21  
**Status:** Approved for implementation planning.  
**Related:** `docs/api-curl.md`, `docs/superpowers/specs/2026-08-21-lan-media-uploads-design.md`

## Problem

The backend is already a FastAPI server, but it was built as a **desktop-private** process:

- Default bind is loopback (`127.0.0.1:41954`).
- Electron injects a **random per-session** `LTX_AUTH_TOKEN`, so scripts and phones cannot keep a stable credential.
- Auth middleware gates **all** HTTP paths, so `/docs` / OpenAPI UI fail in a normal browser without a header extension.
- README / AGENTS still mention port `8000`; there is no supported `pnpm` entrypoint for “run backend without Electron” except `perf:dev` (perf-only).

Users want:

1. **Same-machine** scripts / MCP / curl against the full existing API.
2. **LAN** clients (phone, another PC) against the same Mac/PC GPU.
3. Both a **dedicated headless** process and **opt-in LAN while the desktop app runs**.

## Goals

- Env-first dual mode: headless command **and** Electron with bind/token env passthrough.
- Stable API key via env (`LTX_API_TOKEN`); full existing OpenAPI surface for that key.
- Optional non-loopback bind (`LTX_BIND_HOST=0.0.0.0`) for LAN.
- Browseable Swagger (`/docs`) without a token; `/api/*` still authenticated.
- Document correct host/port and “don’t run app + headless on the same port.”

## Non-goals (v1)

- Settings UI for key / LAN toggle (developer env only).
- Public-internet hardening (TLS termination, rate limits, reverse proxy recipes).
- Per-endpoint LAN ACL or a reduced “client subset” API.
- Docker packaging.
- New generation endpoints (uploads / media refs are covered by the separate LAN uploads spec).
- Changing default packaged-app behavior when no LAN env vars are set.

## Solution (Approach 1 — env-first)

Keep **one** FastAPI process (`ltx2_server.py` → `create_app`). No gateway. Only one process may own the port/GPU at a time.

### Tokens

| Env | Role |
|-----|------|
| `LTX_AUTH_TOKEN` | Desktop **session** secret (Electron generates if unset). Accepted by auth middleware. |
| `LTX_API_TOKEN` | **Stable** key for scripts / LAN. Accepted alongside the session token. |

Auth middleware already accepts either secret (`auth_token` or `api_token`). v1 keeps that dual-accept model.

**LAN rule:** if bind host is not loopback (`127.0.0.1` / `localhost`), refuse to start unless at least one of `LTX_API_TOKEN` or `LTX_AUTH_TOKEN` is non-empty. Prefer documenting `LTX_API_TOKEN` as the LAN credential so Electron’s random session token is not what remote clients depend on.

### Bind

| Env | Default | Meaning |
|-----|---------|---------|
| `LTX_PORT` | `41954` | Already exists |
| `LTX_BIND_HOST` | `127.0.0.1` | Empty/unset → loopback. `0.0.0.0` → all interfaces (LAN) |

Ready log: advertise a client-usable host (when bound to `0.0.0.0`, print `127.0.0.1` for Electron probe compatibility — already the `advertise_bind_host` behavior).

### Run modes

1. **Desktop (unchanged default)** — Electron generates session `LTX_AUTH_TOKEN`, binds loopback unless `LTX_BIND_HOST` is set; forwards `LTX_API_TOKEN` / `LTX_BIND_HOST` / `LTX_PORT` when present.
2. **Headless** — `pnpm backend:serve` runs `ltx2_server.py` via `uv`, reusing app data (`LTX_APP_DATA_DIR` same resolution as `perf:dev` / Electron) so models and settings are shared. Operator sets `LTX_API_TOKEN` (and optionally bind/port).

Do not run headless and the desktop backend on the same port; the second process fails to bind. Document that; no separate lock service.

### Docs paths (unauthenticated)

Skip Bearer/Basic checks for:

- `GET /docs`
- `GET /redoc`
- `GET /openapi.json`
- FastAPI’s `/docs/oauth2-redirect` if present

All other paths (including `/api/*`) keep current auth. Add OpenAPI HTTP Bearer security so Swagger’s **Authorize** matches the middleware.

### CORS

- Loopback bind: keep Vite origins (`localhost:5173`, `127.0.0.1:5173`).
- Non-loopback bind: `allow_origins=["*"]` so LAN browsers can call with `Authorization`. Do not enable credentialed cookies; Bearer-in-header is enough.

### Components to change / finish

Much of bind + dual-token already exists; this design locks the product contract and lists remaining gaps.

| Area | Intent |
|------|--------|
| `ltx2_server.py` / `port_constant.py` | Bind via `LTX_BIND_HOST`; **startup guard** when non-loopback and no token |
| `app_factory.py` | Docs path exemptions; LAN-aware CORS; OpenAPI Bearer scheme |
| `electron/python-backend.ts` | Keep forwarding `LTX_API_TOKEN` / `LTX_BIND_HOST` / `LTX_PORT` (already largely done) |
| `package.json` | Add `backend:serve` |
| Docs | Fix port `8000` mentions; document headless vs app, LAN, `/docs`, curl (`docs/api-curl.md` already started) |
| Tests | Auth exemptions, startup guard, CORS mode, dual-token (partially present) |

### Error handling

| Case | Result |
|------|--------|
| Non-loopback bind + empty tokens | Fail **before** listen with a clear stderr message |
| Missing/wrong token on `/api/*` | Existing `401` JSON |
| Docs / OpenAPI paths | `200` without token |
| Port in use | Existing uvicorn bind failure |

### Testing

1. `/docs` and `/openapi.json` succeed without token when secrets are configured; a sample `/api/*` still `401` without token and `200` with session or API token.
2. Startup helper / guard: non-loopback + empty tokens refuses start (no real `0.0.0.0` listen required in CI).
3. CORS: LAN-mode origins accepted; loopback mode still restricted to Vite list.
4. Dual-token acceptance (existing tests) remain green.
5. Regenerate / `openapi:check` if Bearer security is added to the schema.

## Architecture notes

- **Why one process:** Generation already serializes on one GPU; a second HTTP front would still contend for the same models.
- **Why dual token:** Desktop must keep a session secret the renderer already holds; LAN/scripts need a stable key that survives app restarts (`LTX_API_TOKEN`).
- **Why docs are public, API is not:** Swagger HTML is useless behind 401 without `WWW-Authenticate`; generation and settings mutation must stay keyed, especially on LAN.
- **Why env-only UI:** Matches “developer-only” v1; Settings can come later without changing the wire protocol.
- **Why not auto-LAN when token is set:** Setting a key for local scripts must not silently expose the host on the network.

## Current tree vs this design (snapshot)

Already present (do not re-invent):

- `LTX_BIND_HOST` + `resolve_bind_host` / `advertise_bind_host`
- `LTX_API_TOKEN` accepted beside session `LTX_AUTH_TOKEN`
- Electron env passthrough for bind host and API token
- Curl cookbook: `docs/api-curl.md`

Still required to close this design:

- `pnpm backend:serve`
- Auth skip for `/docs`, `/redoc`, `/openapi.json`
- Startup refuse non-loopback without token
- CORS widen when LAN-bound
- OpenAPI Bearer security + README/AGENTS port and headless docs
- Tests for the above
