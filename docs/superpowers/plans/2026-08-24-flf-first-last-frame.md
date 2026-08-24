# FLF (First Frame Last Frame) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Gen Space local i2v/a2v attach an optional end image (with tunable strength) so generation is conditioned on first and last frames.

**Architecture:** Minimal extension of `GenerateVideoRequest` with `endImagePath` + `endImageStrength`. Local fast and a2v handlers append a second `ImageConditioningInput` at `num_frames - 1`. Gen Space unlocks an end-image slot only after a start image is set and only in local mode; API-only mode rejects/hides FLF.

**Tech Stack:** Python/FastAPI backend (`api_types`, `video_generation_handler`), existing fake pipelines in pytest, React/TypeScript Gen Space + `use-generation`, OpenAPI codegen (`pnpm openapi:generate`), Vitest only if a pure helper is extracted (prefer backend tests + manual UI).

## Global Constraints

- Local generation only (hide UI when `!isLocalMode`; server rejects end image on forced API path).
- End image requires start `imagePath`.
- Start strength stays `1.0`; end strength default `0.8`, range `0.0–1.0`.
- End image resized/cropped via existing `_prepare_image` to the same canvas as start.
- Last frame index: `num_frames - 1` (from `compute_num_frames`); if end image is set, `duration` must be fixed (no auto-duration).
- No dedicated FLF mode or endpoint; no mid-keyframes; enhance still uses start image only.
- Omit `endImagePath` / `endImageStrength` from the wire body when no end image is set.

---

### File map

| File | Responsibility |
|------|----------------|
| `backend/api_types.py` | `endImagePath`, `endImageStrength` on `GenerateVideoRequest` |
| `backend/handlers/video_generation_handler.py` | Validate FLF; dual conditioning on fast + a2v; API reject |
| `backend/tests/test_generation.py` | Validation + dual-image pipeline assertions |
| `frontend/generated/backend-openapi.json` + `.ts` | Regenerated schema/types |
| `frontend/hooks/use-generation.ts` | Pass end fields; recovery type |
| `frontend/types/project-model.ts` | `endImageUrl` / `endImageStrength` on `generationParams` |
| `frontend/views/GenSpace.tsx` | End slot UI, strength slider, wiring, recovery restore |
| `docs/api-curl.md` | Document optional end fields |

---

### Task 1: Request model + early validation

**Files:**
- Modify: `backend/api_types.py` (`GenerateVideoRequest`)
- Modify: `backend/handlers/video_generation_handler.py` (`generate`)
- Test: `backend/tests/test_generation.py`

**Interfaces:**
- Produces: `GenerateVideoRequest.endImagePath: str | None = None`, `GenerateVideoRequest.endImageStrength: float = 0.8` (`ge=0.0`, `le=1.0`)
- Consumes: existing `generate(self, req: GenerateVideoRequest)` entry

- [ ] **Step 1: Write failing validation tests**

Append to `backend/tests/test_generation.py` (reuse `_T2V_JSON`, `_install_local_2_3`, `make_test_image`):

```python
class TestFlfValidation:
    def test_end_image_without_start_returns_400(
        self, client, test_state, create_fake_model_files, make_test_image, tmp_path
    ):
        _install_local_2_3(test_state, create_fake_model_files)
        end = tmp_path / "end.png"
        end.write_bytes(make_test_image().getvalue())
        r = client.post(
            "/api/generate",
            json={**_T2V_JSON, "endImagePath": str(end), "endImageStrength": 0.7},
        )
        assert_http_error(r, status_code=400, code="END_IMAGE_REQUIRES_START")

    def test_end_image_rejected_on_api_only_mode(
        self, client, test_state, make_test_image, tmp_path
    ):
        # Same fixture pattern as test_i2v_routes_to_ltx_api
        test_state.config.local_generations_mode = "unsupported"
        test_state.state.app_settings.ltx_api_key = "api-key"
        start = tmp_path / "start.png"
        end = tmp_path / "end.png"
        start.write_bytes(make_test_image().getvalue())
        end.write_bytes(make_test_image().getvalue())
        r = client.post(
            "/api/generate",
            json={
                "prompt": "Animate this frame",
                "resolution": "2160p",
                "model": "pro",
                "duration": 8,
                "fps": 25,
                "imagePath": str(start),
                "endImagePath": str(end),
            },
        )
        assert_http_error(r, status_code=400, code="END_IMAGE_LOCAL_ONLY")

    def test_flf_rejects_auto_duration_in_generate(
        self, test_state, create_fake_model_files, make_test_image, tmp_path
    ):
        _install_local_2_3(test_state, create_fake_model_files)
        start = tmp_path / "start.png"
        end = tmp_path / "end.png"
        start.write_bytes(make_test_image().getvalue())
        end.write_bytes(make_test_image().getvalue())
        req = GenerateVideoRequest.model_validate({
            **_T2V_JSON,
            "duration": None,
            "imagePath": str(start),
            "endImagePath": str(end),
        })
        with pytest.raises(HTTPError) as exc:
            test_state.video_generation.generate(req)
        assert exc.value.status_code == 400
        assert exc.value.code == "END_IMAGE_REQUIRES_DURATION"
```

Run FLF checks in `generate()` **before** `validate_generate_video_request` so auto-duration still surfaces `END_IMAGE_REQUIRES_DURATION` even when the model would otherwise reject `duration=None`.

- [ ] **Step 2: Run tests — expect fail**

```bash
cd backend && uv run pytest tests/test_generation.py::TestFlfValidation -v --tb=short
```

Expected: FAIL (missing fields / codes).

- [ ] **Step 3: Add fields to `GenerateVideoRequest`**

In `backend/api_types.py`, inside `GenerateVideoRequest` after `imagePath`:

```python
    imagePath: str | None = None
    endImagePath: str | None = None
    endImageStrength: float = Field(default=0.8, ge=0.0, le=1.0)
    audioPath: str | None = None
```

- [ ] **Step 4: Early checks in `VideoGenerationHandler.generate`**

Run FLF checks **before** `validate_generate_video_request` so auto-duration never masks `END_IMAGE_REQUIRES_DURATION`:

```python
    def generate(self, req: GenerateVideoRequest) -> GenerateVideoResponse:
        use_api_specs = should_video_generate_with_ltx_api(
            force_api_generations=self.config.force_api_generations,
            settings=self.state.app_settings,
        )

        end_image_path = self._normalize_media_path(req.endImagePath)
        if end_image_path is not None:
            if self._normalize_media_path(req.imagePath) is None:
                raise HTTPError(
                    400,
                    "END_IMAGE_REQUIRES_START",
                    code="END_IMAGE_REQUIRES_START",
                )
            if use_api_specs:
                raise HTTPError(
                    400,
                    "END_IMAGE_LOCAL_ONLY",
                    code="END_IMAGE_LOCAL_ONLY",
                )
            if req.duration is None:
                raise HTTPError(
                    400,
                    "END_IMAGE_REQUIRES_DURATION",
                    code="END_IMAGE_REQUIRES_DURATION",
                )

        validation_error = validate_generate_video_request(
            req,
            use_api_specs=use_api_specs,
            local_model_id=None if use_api_specs else self._active_ltx_model_id(),
            duration_head_ready=False if use_api_specs else self._duration_head_ready(),
        )
        # ... rest unchanged
```

Use `detail` equal to `code` so `assert_http_error` (which defaults `message` to `code`) matches without a custom message.

- [ ] **Step 5: Re-run validation tests — expect pass**

```bash
cd backend && uv run pytest tests/test_generation.py::TestFlfValidation -v --tb=short
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/api_types.py backend/handlers/video_generation_handler.py backend/tests/test_generation.py
git commit -m "feat(flf): add endImagePath validation for local-only FLF"
```

---

### Task 2: Dual image conditioning on local i2v + a2v

**Files:**
- Modify: `backend/handlers/video_generation_handler.py` (`_run_queued_job`, `generate_video`, `_generate_a2v`)
- Test: `backend/tests/test_generation.py`

**Interfaces:**
- Consumes: `req.endImagePath`, `req.endImageStrength`, `_prepare_image`, `compute_num_frames` / resolved `num_frames`
- Produces: `images` list with start `@0/1.0` and end `@num_frames-1/endImageStrength` when end set

- [ ] **Step 1: Write failing pipeline tests**

```python
class TestFlfConditioning:
    def test_i2v_passes_start_and_end_conditioning(
        self, client, test_state, fake_services, create_fake_model_files, make_test_image, tmp_path
    ):
        _install_local_2_3(test_state, create_fake_model_files)
        start = tmp_path / "start.png"
        end = tmp_path / "end.png"
        start.write_bytes(make_test_image().getvalue())
        end.write_bytes(make_test_image().getvalue())
        r = client.post(
            "/api/generate",
            json={
                **_T2V_JSON,
                "imagePath": str(start),
                "endImagePath": str(end),
                "endImageStrength": 0.65,
            },
        )
        assert r.status_code == 200
        call = fake_services.fast_video_pipeline.generate_calls[0]
        images = call["images"]
        assert len(images) == 2
        assert images[0].frame_idx == 0
        assert images[0].strength == 1.0
        expected_frames = test_state.video_generation._compute_num_frames(5, 24)
        assert images[1].frame_idx == expected_frames - 1
        assert images[1].strength == 0.65

    def test_a2v_passes_start_and_end_conditioning(
        self, client, test_state, fake_services, create_fake_model_files, make_test_image, tmp_path
    ):
        _install_local_2_3(test_state, create_fake_model_files)
        start = tmp_path / "start.png"
        end = tmp_path / "end.png"
        audio = tmp_path / "a.wav"
        start.write_bytes(make_test_image().getvalue())
        end.write_bytes(make_test_image().getvalue())
        _write_test_wav(audio)
        r = client.post(
            "/api/generate",
            json={
                **_T2V_JSON,
                "imagePath": str(start),
                "endImagePath": str(end),
                "endImageStrength": 0.9,
                "audioPath": str(audio),
            },
        )
        assert r.status_code == 200
        call = fake_services.a2v_pipeline.generate_calls[0]
        images = call["images"]
        assert len(images) == 2
        assert images[0].frame_idx == 0 and images[0].strength == 1.0
        expected_frames = test_state.video_generation._compute_num_frames(5, 24)
        assert images[1].frame_idx == expected_frames - 1
        assert images[1].strength == 0.9

    def test_i2v_start_only_still_single_conditioning(
        self, client, test_state, fake_services, create_fake_model_files, make_test_image, tmp_path
    ):
        _install_local_2_3(test_state, create_fake_model_files)
        start = tmp_path / "start.png"
        start.write_bytes(make_test_image().getvalue())
        r = client.post("/api/generate", json={**_T2V_JSON, "imagePath": str(start)})
        assert r.status_code == 200
        images = fake_services.fast_video_pipeline.generate_calls[0]["images"]
        assert len(images) == 1
        assert images[0].frame_idx == 0
        assert images[0].strength == 1.0
```

If a2v needs an installed audio-capable local model in fixtures, mirror the existing `test_a2v_with_image_uses_i2v_setting` setup exactly.

- [ ] **Step 2: Run tests — expect fail**

```bash
cd backend && uv run pytest tests/test_generation.py::TestFlfConditioning -v --tb=short
```

Expected: FAIL (only one conditioning / end ignored).

- [ ] **Step 3: Extend `generate_video` to accept optional end image**

Change signature and body in `video_generation_handler.py`:

```python
    def generate_video(
        self,
        prompt: str,
        enhance_via_api: bool,
        image: Image.Image | None,
        height: int,
        width: int,
        num_frames: int | AutoDurationSpec,
        fps: float,
        seed: int,
        camera_motion: VideoCameraMotion,
        negative_prompt: str,
        loras: list[tuple[str, float]] | None = None,
        end_image: Image.Image | None = None,
        end_image_strength: float = 0.8,
    ) -> str:
```

Replace single-temp logic with:

```python
        images: list[ImageConditioningInput] = []
        temp_image_paths: list[str] = []
        try:
            if image is not None:
                temp_start = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
                image.save(temp_start)
                temp_image_paths.append(temp_start)
                images.append(ImageConditioningInput(path=temp_start, frame_idx=0, strength=1.0))

            if end_image is not None:
                if not isinstance(num_frames, int):
                    raise HTTPError(
                        400,
                        "End-frame conditioning requires a fixed duration",
                        code="END_IMAGE_REQUIRES_DURATION",
                    )
                temp_end = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
                end_image.save(temp_end)
                temp_image_paths.append(temp_end)
                images.append(
                    ImageConditioningInput(
                        path=temp_end,
                        frame_idx=num_frames - 1,
                        strength=end_image_strength,
                    )
                )
            # ... existing generate call unchanged except images=images ...
        finally:
            self._text.clear_api_embeddings()
            for path in temp_image_paths:
                if os.path.exists(path):
                    os.unlink(path)
```

- [ ] **Step 4: Prepare end image in `_run_queued_job` before `generate_video`**

Where start image is prepared:

```python
            image = None
            end_image = None
            image_path = self._normalize_media_path(req.imagePath)
            if image_path:
                image = self._prepare_image(image_path, width, height)
                logger.info("Image: %s -> %sx%s", image_path, width, height)

            end_path = self._normalize_media_path(req.endImagePath)
            if end_path:
                end_image = self._prepare_image(end_path, width, height)
                logger.info("End image: %s -> %sx%s", end_path, width, height)

            # pass into generate_video(... end_image=end_image, end_image_strength=req.endImageStrength)
```

- [ ] **Step 5: Same dual conditioning in `_generate_a2v`**

After preparing start `image`, also:

```python
        end_image = None
        end_path = self._normalize_media_path(req.endImagePath)
        if end_path:
            end_image = self._prepare_image(end_path, width, height)

        # when building images list:
            temp_paths: list[str] = []
            if image is not None:
                temp_start = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
                image.save(temp_start)
                temp_paths.append(temp_start)
                images.append(ImageConditioningInput(path=temp_start, frame_idx=0, strength=1.0))
            if end_image is not None:
                temp_end = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
                end_image.save(temp_end)
                temp_paths.append(temp_end)
                images.append(
                    ImageConditioningInput(
                        path=temp_end,
                        frame_idx=num_frames - 1,
                        strength=req.endImageStrength,
                    )
                )
        # finally: unlink all temp_paths (replace single temp_image_path cleanup)
```

- [ ] **Step 6: Re-run conditioning tests — expect pass**

```bash
cd backend && uv run pytest tests/test_generation.py::TestFlfConditioning tests/test_generation.py::TestFlfValidation -v --tb=short
```

Expected: PASS. Also smoke existing i2v/a2v tests:

```bash
cd backend && uv run pytest tests/test_generation.py -k "i2v or a2v" -v --tb=short
```

- [ ] **Step 7: Commit**

```bash
git add backend/handlers/video_generation_handler.py backend/tests/test_generation.py
git commit -m "feat(flf): condition local i2v/a2v on first and last frames"
```

---

### Task 3: Regenerate OpenAPI types

**Files:**
- Modify: `frontend/generated/backend-openapi.json`
- Modify: `frontend/generated/backend-openapi.ts`

**Interfaces:**
- Produces: `components['schemas']['GenerateVideoRequest']` includes `endImagePath?`, `endImageStrength?`

- [ ] **Step 1: Regenerate**

```bash
pnpm openapi:generate
```

- [ ] **Step 2: Verify schema contains new fields**

```bash
rg -n "endImagePath|endImageStrength" frontend/generated/backend-openapi.json frontend/generated/backend-openapi.ts | head
```

Expected: both fields present on `GenerateVideoRequest`.

- [ ] **Step 3: Commit**

```bash
git add frontend/generated/backend-openapi.json frontend/generated/backend-openapi.ts
git commit -m "chore: regenerate OpenAPI types for FLF fields"
```

---

### Task 4: Frontend hook, recovery, and project params

**Files:**
- Modify: `frontend/hooks/use-generation.ts`
- Modify: `frontend/types/project-model.ts`
- Optional create: `frontend/lib/flf-generate-body.ts` + `frontend/lib/flf-generate-body.test.ts` (pure helper to keep GenSpace thin)

**Interfaces:**
- Produces:
  - `generate(..., endImagePath?: string | null, endImageStrength?: number)`
  - `GenerationRecoveryContext.endImageUrl?: string`
  - `GenerationRecoveryContext.endImageStrength?: number`
  - `generationParams.endImageUrl?: string`, `generationParams.endImageStrength?: number`

- [ ] **Step 1: Extend Zod `generationParams`**

In `frontend/types/project-model.ts`, next to `inputImageUrl`:

```ts
  inputImageUrl: z.string().optional(),
  endImageUrl: z.string().optional(),
  endImageStrength: z.number().optional(),
  inputAudioUrl: z.string().optional(),
```

- [ ] **Step 2: Extend recovery context + `generate` signature**

In `use-generation.ts`:

```ts
export interface GenerationRecoveryContext {
  // ...existing...
  inputImageUrl?: string
  endImageUrl?: string
  endImageStrength?: number
  inputAudioUrl?: string
  // ...
}

  generate: (
    prompt: string,
    imagePath: string | null,
    settings: GenerationSettings,
    audioPath?: string | null,
    projectName?: string | null,
    endImagePath?: string | null,
    endImageStrength?: number,
  ) => Promise<void>
```

Inside `generate`, after setting `imagePath`:

```ts
        if (imagePath) {
          body.imagePath = imagePath
        }
        if (imagePath && endImagePath) {
          body.endImagePath = endImagePath
          body.endImageStrength = endImageStrength ?? 0.8
        }
```

Do **not** send end fields without a start path.

- [ ] **Step 3 (optional but preferred): Pure helper + Vitest**

`frontend/lib/flf-generate-body.ts`:

```ts
export function applyFlfFields(
  body: Record<string, unknown>,
  imagePath: string | null | undefined,
  endImagePath: string | null | undefined,
  endImageStrength?: number,
): void {
  if (!imagePath || !endImagePath) return
  body.endImagePath = endImagePath
  body.endImageStrength = endImageStrength ?? 0.8
}
```

Test:

```ts
import { describe, expect, it } from 'vitest'
import { applyFlfFields } from './flf-generate-body'

describe('applyFlfFields', () => {
  it('no-ops without end', () => {
    const body: Record<string, unknown> = {}
    applyFlfFields(body, '/a.png', null)
    expect(body).toEqual({})
  })
  it('no-ops without start', () => {
    const body: Record<string, unknown> = {}
    applyFlfFields(body, null, '/b.png')
    expect(body).toEqual({})
  })
  it('sets path and default strength', () => {
    const body: Record<string, unknown> = {}
    applyFlfFields(body, '/a.png', '/b.png')
    expect(body).toEqual({ endImagePath: '/b.png', endImageStrength: 0.8 })
  })
})
```

```bash
pnpm test:frontend -- frontend/lib/flf-generate-body.test.ts
```

- [ ] **Step 4: Commit**

```bash
git add frontend/hooks/use-generation.ts frontend/types/project-model.ts frontend/lib/flf-generate-body.ts frontend/lib/flf-generate-body.test.ts
git commit -m "feat(flf): wire end image fields through generate and project params"
```

---

### Task 5: Gen Space UI

**Files:**
- Modify: `frontend/views/GenSpace.tsx` (`PromptBar` + GenSpace state / generate / recovery / asset `generationParams`)

**Interfaces:**
- Consumes: `isLocalMode`, `inputImage`, `generate(..., endImagePath, endImageStrength)`
- Produces: end slot + strength slider; clears end when start cleared

- [ ] **Step 1: Add state**

Near `inputImage` state:

```ts
  const [endImage, setEndImage] = useState<string | null>(null)
  const [endImageStrength, setEndImageStrength] = useState(0.8)
```

When clearing start image (any `setInputImage(null)` path that means user cleared start), also:

```ts
setEndImage(null)
setEndImageStrength(0.8)
```

Wrap start clear in a helper used by PromptBar callback:

```ts
  const handleInputImageChange = (path: string | null) => {
    setInputImage(path)
    if (!path) {
      setEndImage(null)
      setEndImageStrength(0.8)
    }
  }
```

- [ ] **Step 2: Extend `PromptBar` props**

Add:

```ts
  endImage: string | null
  onEndImageChange: (path: string | null) => void
  endImageStrength: number
  onEndImageStrengthChange: (v: number) => void
```

UI (video mode, not retake/ic-lora): after the start-image drop zone, if `inputImage && isLocalMode`, render a second drop zone (same pattern as start; label/title `"Last frame"`). Clear button only clears end.

In advanced/settings row (same pattern as image-edit strength around the existing strength slider), when `endImage && isLocalMode`:

```tsx
<div className="flex items-center gap-2 text-[10px] text-zinc-400 px-2">
  <span>End frame</span>
  <input
    type="range"
    min={0}
    max={1}
    step={0.05}
    value={endImageStrength}
    onChange={(e) => onEndImageStrengthChange(parseFloat(e.target.value))}
  />
  <span className="w-8 text-right">{endImageStrength.toFixed(2)}</span>
</div>
```

- [ ] **Step 3: Wire generate + recovery + asset params**

In video generate path:

```ts
      generate(prompt, imagePath, genSettings, audioPath, activeProject?.name, endImage, endImage ? endImageStrength : undefined)
```

`writeRecoveryContext` for video:

```ts
          inputImageUrl: imagePath ?? undefined,
          endImageUrl: endImage ?? undefined,
          endImageStrength: endImage ? endImageStrength : undefined,
          inputAudioUrl: audioPath ?? undefined,
```

On recovery restore (where `setInputImage(ctx.inputImageUrl ?? null)`):

```ts
        setEndImage(ctx.endImageUrl ?? null)
        setEndImageStrength(ctx.endImageStrength ?? 0.8)
```

When writing `generationParams` for the completed video asset:

```ts
            inputImageUrl: inputImage || undefined,
            endImageUrl: endImage || undefined,
            endImageStrength: endImage ? endImageStrength : undefined,
            inputAudioUrl: inputAudio || undefined,
```

Pass new props into `<PromptBar ... />`.

- [ ] **Step 4: Manual checklist (document in commit body or leave for QA)**

- Local mode: start → end slot appears; clear start → end clears.
- API-only: end slot absent.
- Generate i2v with end; generate a2v with start+end+audio.
- Strength slider changes payload (devtools / backend log).

- [ ] **Step 5: Commit**

```bash
git add frontend/views/GenSpace.tsx
git commit -m "feat(flf): Gen Space end-frame slot and strength control"
```

---

### Task 6: Curl docs

**Files:**
- Modify: `docs/api-curl.md`

- [ ] **Step 1: Document optional fields**

Where optional generate fields are listed (`imagePath`, `audioPath`, …), add:

```markdown
Optional fields: `imagePath`, `endImagePath`, `endImageStrength` (default `0.8`, requires `imagePath`; **local generation only**), `audioPath`, `seed`, `loras`, `negativePrompt`.

FLF: set both `imagePath` (first frame) and `endImagePath` (last frame). Requires a fixed `duration` (not automatic). Rejected when the server is in API-only / forced-API mode.
```

Add a short curl example after the existing i2v upload example if one exists; otherwise extend the generate JSON sample.

- [ ] **Step 2: Commit**

```bash
git add docs/api-curl.md
git commit -m "docs: document FLF endImagePath on /api/generate"
```

---

### Spec coverage (self-review)

| Spec requirement | Task |
|------------------|------|
| Gen Space optional end after start | Task 5 |
| Local i2v + a2v dual conditioning | Task 2 |
| Start strength 1.0; end slider default 0.8 | Tasks 2, 5 |
| Resize end to start canvas via `_prepare_image` | Task 2 |
| Recovery + `generationParams` provenance | Tasks 4–5 |
| API-only hide + server reject | Tasks 1, 5 |
| Fixed duration when end set | Task 1 |
| `frame_idx = num_frames - 1` | Task 2 |
| No cloud FLF / no new mode / enhance start-only | Non-goals honored (no tasks) |
| OpenAPI + curl docs | Tasks 3, 6 |
| Tests: validation, dual conditioning, start-only regression | Tasks 1–2 |
| Frontend payload helper | Task 4 |

**Type names:** `endImagePath` / `endImageStrength` (API), `endImageUrl` / `endImageStrength` (recovery + generationParams), UI state `endImage` / `endImageStrength` — consistent across tasks.
