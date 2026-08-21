# LTX Desktop — Generate API (curl)

Start headless with `LTX_API_TOKEN=… pnpm backend:serve`, or set `LTX_API_TOKEN` /
`LTX_BIND_HOST` when launching the desktop app. Interactive docs: `$LTX_HOST/docs`
(Swagger loads without a token; click Authorize and paste the Bearer token).

Replace the host and token with yours (Dev Panel / `LTX_API_TOKEN`).

```bash
export LTX_HOST="http://m5max.local:41954"
export LTX_API_TOKEN="your-token-here"
```

`cameraMotion` must be one of: `none`, `dolly_in`, `dolly_out`, `pan_left`, `pan_right`, etc. — not `"dynamic"`.

---

## Generate (with `projectName` — queues, returns immediately)

```bash
curl -sS "$LTX_HOST/api/generate" \
  -H "authorization: Bearer $LTX_API_TOKEN" \
  -H "content-type: application/json" \
  -d '{
    "prompt": "a cat walking on the moon",
    "model": "fast",
    "duration": 5,
    "fps": 24,
    "resolution": "540p",
    "cameraMotion": "none",
    "audio": false,
    "aspectRatio": "16:9",
    "projectName": "My Project"
  }'
```

**Response:**

```json
{ "status": "queued", "id": "abc12345" }
```

The job runs in the background. There is **no** `video_path` / `video_url` in this response — poll until the job finishes (see [Getting the video after a queued generate](#getting-the-video-after-a-queued-generate)).

The desktop app also imports the finished clip into the named project.

---

## Getting the video after a queued generate

Save the `id` from the queued response, then poll **`GET /api/project-ingest/{id}`** until `status` is `complete`:

```bash
JOB_ID="abc12345"   # from generate response

while true; do
  JOB=$(curl -sS "$LTX_HOST/api/project-ingest/$JOB_ID" \
    -H "authorization: Bearer $LTX_API_TOKEN")
  echo "$JOB"
  STATUS=$(echo "$JOB" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
  if [ "$STATUS" = "complete" ]; then
    break
  fi
  sleep 2
done

VIDEO_URL=$(echo "$JOB" | python3 -c "import sys,json; print(json.load(sys.stdin)['video_url'])")
curl -sS -o out.mp4 "$LTX_HOST$VIDEO_URL" \
  -H "authorization: Bearer $LTX_API_TOKEN"
```

**Queued response:**

```json
{
  "id": "abc12345",
  "status": "queued",
  "video_path": "",
  "video_url": null,
  "projectName": "My Project",
  "prompt": "..."
}
```

**Complete response:**

```json
{
  "id": "abc12345",
  "status": "complete",
  "video_path": "/path/to/ltx2_video_….mp4",
  "video_url": "/api/outputs/ltx2_video_….mp4",
  "projectName": "My Project",
  "prompt": "..."
}
```

`404` only if the id was never created (or you cancelled a still-queued job). After the desktop imports the clip, the job leaves the list but **`GET` by id still returns `complete` + `video_url`**.

| Source | When | What you get |
|--------|------|----------------|
| `POST /api/generate` + `projectName` | immediately | `{ status: "queued", id }` only |
| `GET /api/project-ingest/{id}` | anytime | that job; `video_url` when `status` is `complete` |
| `GET /api/project-ingest` | anytime | all jobs; empty `video_path` until each finishes |
| `GET /api/generation/progress` | anytime | **only the current GPU slot** (see below) |
| `GET /api/outputs/{filename}` | after file exists | download bytes |

### Progress API with a queue

There is **one** generation slot. `/api/generation/progress` always describes that slot — not a specific queued job:

| Your job | What progress shows |
|----------|---------------------|
| Still waiting in queue | Another job’s `id` (the one currently running), or sticky `complete` from a previous job |
| Currently running | Your `id`, `status: "running"`, live `phase` / `progress` |
| Finished | May briefly show your `complete` + `result`, then switch to the **next** queued job’s `running` |

So if you enqueue A → B → C:

1. Progress `id` = A while A runs  
2. When A finishes, progress may show A `complete`, then quickly B `running`  
3. It will **not** keep reporting A after B has started  

**Rules of thumb:**

- Wait for **your** video with **`GET /api/project-ingest/{id}`** until `status` is `complete` (use `video_url`).
- Use progress only when `progress.id === yourJobId` (then `phase` / `progress` are for you).
- Do not assume progress `result` is your file if several jobs were queued.

---

## Generate (no `projectName` — blocks until done)

```bash
curl -sS "$LTX_HOST/api/generate" \
  -H "authorization: Bearer $LTX_API_TOKEN" \
  -H "content-type: application/json" \
  -d '{
    "prompt": "a cat walking on the moon",
    "model": "fast",
    "duration": 5,
    "fps": 24,
    "resolution": "540p",
    "cameraMotion": "none",
    "audio": false,
    "aspectRatio": "16:9"
  }'
```

**Response:**

```json
{
  "status": "complete",
  "video_path": "/path/to/ltx2_video_....mp4",
  "video_url": "/api/outputs/ltx2_video_....mp4"
}
```

Optional fields: `imagePath`, `audioPath`, `seed`, `loras`, `negativePrompt`.

For LAN clients, do **not** pass host filesystem paths. Upload the file first, then put the returned `url` into `imagePath` or `audioPath` (see [Upload image or audio](#upload-image-or-audio)).

---

## Upload image or audio

LAN / remote clients cannot use absolute paths on the machine running LTX. Upload the media, then pass the opaque `url` as `imagePath` or `audioPath` on generate.

### `POST /api/uploads`

Multipart form: required `file`, optional `kind` (`image` | `audio`). If `kind` is omitted, the server infers from content-type / extension; if still ambiguous → `400`.

Limits: image ≤ 50MB, audio ≤ 100MB. Uploads expire after **~24 hours** (swept on start and on each successful upload). There is no delete endpoint in v1.

```bash
# Image (i2v)
UPLOAD=$(curl -sS "$LTX_HOST/api/uploads" \
  -H "authorization: Bearer $LTX_API_TOKEN" \
  -F "file=@./start-frame.png" \
  -F "kind=image")
echo "$UPLOAD"
IMAGE_URL=$(echo "$UPLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['url'])")
# → /api/uploads/<id>.png
```

```bash
# Audio (a2v)
UPLOAD=$(curl -sS "$LTX_HOST/api/uploads" \
  -H "authorization: Bearer $LTX_API_TOKEN" \
  -F "file=@./voice.wav" \
  -F "kind=audio")
AUDIO_URL=$(echo "$UPLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['url'])")
```

**Response:**

```json
{ "url": "/api/uploads/a1b2c3d4.png" }
```

### Generate with the upload URL

```bash
curl -sS "$LTX_HOST/api/generate" \
  -H "authorization: Bearer $LTX_API_TOKEN" \
  -H "content-type: application/json" \
  -d "{
    \"prompt\": \"camera slowly pushes in\",
    \"model\": \"fast\",
    \"duration\": 5,
    \"fps\": 24,
    \"resolution\": \"540p\",
    \"cameraMotion\": \"dolly_in\",
    \"imagePath\": \"$IMAGE_URL\",
    \"audio\": false,
    \"aspectRatio\": \"16:9\"
  }"
```

Accepted for `imagePath` / `audioPath`:

| Value | Behavior |
|-------|----------|
| Absolute path on the host | Local/desktop only |
| `/api/uploads/<filename>` | LAN-safe ref from `POST /api/uploads` |
| `http://…`, `/api/outputs/…`, etc. | `400` |

### `GET /api/uploads/{filename}`

Download a previously uploaded file (auth required). `404` if missing, expired, or path traversal.

```bash
curl -sS -o uploaded.png \
  "$LTX_HOST$IMAGE_URL" \
  -H "authorization: Bearer $LTX_API_TOKEN"
```

---

## Generation progress

```bash
curl -sS "$LTX_HOST/api/generation/progress" \
  -H "authorization: Bearer $LTX_API_TOKEN"
```

Useful fields: `status` (`running` | `complete` | `idle` | …), `phase`, `progress`, `id`, `result`.

With a queue, this is **only the job on the GPU right now** (or the last sticky completion). Compare `id` to the `id` from your generate response; if they differ, your job is still queued or already finished — use project-ingest for completion.

---

## Cancel (stops the running GPU job)

```bash
curl -sS -X POST "$LTX_HOST/api/generate/cancel" \
  -H "authorization: Bearer $LTX_API_TOKEN"
```

To remove a **queued** ingest job (not yet running) when using `projectName`:

```bash
curl -sS -X DELETE "$LTX_HOST/api/project-ingest/JOB_ID" \
  -H "authorization: Bearer $LTX_API_TOKEN"
```

---

## Get one ingest job by id (video URL)

```bash
curl -sS "$LTX_HOST/api/project-ingest/JOB_ID" \
  -H "authorization: Bearer $LTX_API_TOKEN"
```

Returns `status: "queued"` with `video_url: null`, or `status: "complete"` with `video_url` ready to download via `/api/outputs/...`.

---

## List project ingest queue (when using `projectName`)

```bash
curl -sS "$LTX_HOST/api/project-ingest" \
  -H "authorization: Bearer $LTX_API_TOKEN"
```

Pending jobs have `"video_path": ""`. When generation finishes, `video_path` is filled (or use get-by-id for `video_url`).

---

## Download video output

From `video_url` (or the basename of `video_path`):

```bash
curl -sS -o out.mp4 \
  "$LTX_HOST/api/outputs/ltx2_video_20260820_123456_abcd1234.mp4" \
  -H "authorization: Bearer $LTX_API_TOKEN"
```

### One-liner after a blocking generate

```bash
RESP=$(curl -sS "$LTX_HOST/api/generate" \
  -H "authorization: Bearer $LTX_API_TOKEN" \
  -H "content-type: application/json" \
  -d '{
    "prompt": "moon landing",
    "model": "fast",
    "duration": 5,
    "fps": 24,
    "resolution": "540p",
    "cameraMotion": "none"
  }')
echo "$RESP"
URL=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['video_url'])")
curl -sS -o out.mp4 "$LTX_HOST$URL" -H "authorization: Bearer $LTX_API_TOKEN"
```

---

## Quick reference

| Action | Method | Path |
|--------|--------|------|
| Generate video | `POST` | `/api/generate` |
| Upload image/audio | `POST` | `/api/uploads` |
| Download upload | `GET` | `/api/uploads/{filename}` |
| Progress | `GET` | `/api/generation/progress` |
| Cancel running job | `POST` | `/api/generate/cancel` |
| Get ingest job (video URL) | `GET` | `/api/project-ingest/{job_id}` |
| List ingest queue | `GET` | `/api/project-ingest` |
| Cancel/remove queued ingest | `DELETE` | `/api/project-ingest/{job_id}` |
| Download output file | `GET` | `/api/outputs/{filename}` |
