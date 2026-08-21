# LAN media uploads — design

**Date:** 2026-08-21  
**Status:** Implemented.

## Problem

LAN clients call `/api/generate` with `imagePath` / `audioPath`, but those fields today expect **local filesystem paths on the host** running LTX Desktop. Remote machines cannot supply usable paths. Absolute host paths in API responses also leak host layout.

## Goals

- Let LAN clients upload image and audio files over the authenticated HTTP API.
- Return an opaque API URL (not an absolute filesystem path) that can be passed as `imagePath` / `audioPath`.
- Keep local/desktop generate working with absolute filesystem paths.
- Soft-delete unused uploads after **1 day**.

## Non-goals (v1)

- Frontend UI for LAN upload.
- Video upload.
- Explicit `DELETE` API.
- Accepting full `http(s)://…` URLs as generate media refs.
- Changing `/api/outputs` semantics for generated videos.
- Extending TTL when a generate job is queued against an upload.

## Solution

Dedicated uploads store under app data, `POST /api/uploads` + `GET /api/uploads/{filename}`, and a shared media-ref resolver used by video generate so `imagePath` / `audioPath` accept either an absolute path or `/api/uploads/<id>.<ext>`.

### API

#### `POST /api/uploads`

- Auth: same Bearer token as other LAN APIs (`LTX_API_TOKEN` / session).
- Body: `multipart/form-data`
  - `file` (required)
  - `kind` (optional): `image` | `audio`. If omitted, infer from content-type / extension; if still ambiguous → `400`.
- Response `200`:

```json
{ "url": "/api/uploads/<id>.<ext>" }
```

- Size limits: reuse existing caps after save (image 50MB, audio 100MB). Unsupported or invalid media → `400` via existing validators where applicable.
- Filename on disk: opaque id + safe extension derived from kind/content; never use the client-supplied basename as the stored name.

#### `GET /api/uploads/{filename}`

- Auth required.
- Serves the stored file if present; `404` if missing/expired.
- Path resolution must reject traversal (`..`, absolute segments) the same way as `/api/outputs/{filename}`.

### Generate dual acceptance

For `imagePath` and `audioPath` on `/api/generate` (and any handler that already normalizes those fields the same way):

| Input | Behavior |
|-------|----------|
| Absolute filesystem path | Unchanged: validate file on disk |
| `/api/uploads/<filename>` | Resolve under `app_data/uploads/` only, then same validators |
| Other URL-like values (`http://…`, `/api/outputs/…`, etc.) | `400` |

Desktop/local callers keep passing absolute paths. LAN callers: upload → put `url` into `imagePath` or `audioPath`.

### Storage & TTL

- Directory: `RuntimeConfig.app_data_dir / "uploads"`.
- TTL: **24 hours** from file mtime.
- Sweep expired files on:
  - backend / app start
  - each successful `POST /api/uploads`
- No delete endpoint in v1. If a file expires before generate runs, generate fails with the existing file-not-found style error.

### Architecture

Follow existing backend layering:

1. `_routes/uploads.py` — thin POST/GET plumbing.
2. `handlers/uploads_handler.py` — write file, build public URL, resolve filename, TTL sweep.
3. Shared resolver `resolve_media_ref(value)` used by video generation before `validate_image_file` / `validate_audio_file`: maps `/api/uploads/…` → absolute path under uploads dir; passes through absolute paths; rejects other URL-like values. Keep `normalize_optional_path` for empty/whitespace only.
4. Wire handler on `AppHandler` / service bundle as needed; OpenAPI regenerate; document curl in `docs/api-curl.md`.

### Error summary

| Case | Result |
|------|--------|
| Missing / empty `file` | `400` |
| Ambiguous or conflicting `kind` | `400` |
| Invalid / unsupported media | `400` |
| Over size limit | `400` or `413` |
| Auth failure | `401` |
| GET unknown / expired / traversal | `404` |
| Generate with bad or expired upload URL | `400` (file not found) |

### Testing

Integration tests (Starlette TestClient, fakes, no `unittest.mock`):

1. Upload image → response `url` matches `/api/uploads/…`.
2. Generate with that `url` as `imagePath` succeeds (resolved on host).
3. Absolute `imagePath` / `audioPath` still work.
4. Traversal / escape attempts on upload id rejected.
5. Files older than 24h removed by sweep; fresh files kept.
6. `GET /api/uploads/{filename}` returns bytes when present.

### Docs

Add curl flow to `docs/api-curl.md`: upload → copy `url` into generate `imagePath` or `audioPath`.

## Architecture notes

- **Why URL not absolute path:** LAN responses must not expose host filesystem layout; opaque `/api/uploads/…` refs stay behind auth and a chrooted resolve.
- **Why dual acceptance:** Desktop already uses absolute paths; changing only LAN callers avoids breaking Electron flows.
- **Why dedicated uploads dir:** Keeps uploaded refs separate from generated `/api/outputs` artifacts and simplifies TTL cleanup.
- **Why sweep on upload + start:** Cheap, no background scheduler required for v1; 1-day retention is approximate (mtime-based).
