# FLF (First Frame Last Frame) — design

**Date:** 2026-08-24  
**Status:** Approved (pending implementation plan).

## Problem

Gen Space image-to-video conditions on a single start image at frame 0. Users who want a video that begins on one still and ends on another must hope the prompt alone lands the ending. The local LTX pipelines already accept a list of image conditionings with `frame_idx` and `strength`; the desktop app never exposes a last-frame slot.

## Goals

- Extend Gen Space **video** mode so that, after a start image is set, the user can attach an optional **end image**.
- Pass both frames into **local** i2v and **local** a2v generation.
- Keep start-frame strength hard-pinned at `1.0`; expose an advanced **end-frame strength** slider.
- Resize/crop the end image to the canvas derived from the start image (same sizing rules as today’s single-image i2v).
- Persist end image + strength in generation recovery / project context so interrupted runs can restore FLF inputs.

## Non-goals (v1)

- Cloud / LTX API FLF (hide or reject end image in API-only mode).
- A dedicated Gen Space mode or separate `/api/generate/flf` endpoint.
- Mid-clip keyframes or a general N-image keyframe API.
- Video Editor / timeline gap-fill FLF.
- Dual-image prompt enhancement (enhance continues to use the start image only).
- User-adjustable start-frame strength.

## Solution

Minimal extension of the existing generate path: optional `endImagePath` + `endImageStrength` on `GenerateVideoRequest`. The UI treats FLF as “i2v with an optional last frame,” not a new generation type.

### Approach

Chosen: **minimal i2v extension** (not a generalized keyframe list, not a separate endpoint).

### API

Extend `GenerateVideoRequest` (`backend/api_types.py`):

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `endImagePath` | `str \| None` | `None` | Path to last-frame image; requires `imagePath` |
| `endImageStrength` | `float` | `0.8` | Range `0.0–1.0`; applied only when `endImagePath` is set |

Validation:

- `endImagePath` without `imagePath` → `400`.
- Invalid/missing end file → same error shape as a bad `imagePath`.
- End image on the cloud/API generate path → reject with a clear local-only error (UI should not offer the control).
- When end image is set and duration would be auto/unknown before run, require a fixed duration so `frame_idx` for the last frame is knowable at request time.

### Pipeline mapping

In local `_generate_fast` and `_generate_a2v` (`video_generation_handler.py`):

1. Start: `ImageConditioningInput(path=start, frame_idx=0, strength=1.0)` (unchanged).
2. End (if present): after resolving `num_frames`, append  
   `ImageConditioningInput(path=end, frame_idx=num_frames - 1, strength=endImageStrength)`.  
   Spec default is last pixel frame = `num_frames - 1`. If pipeline/integration tests show the stack expects a different terminal index, adjust once in the handler and update this spec — do not invent a second UI control for it.
3. Preprocess end image to the start-derived canvas (resize/crop) before writing the temp conditioning path.

Start-only requests remain a single-element `images` list — no behavior change.

### Frontend (Gen Space)

- End-image slot appears **only after** a start image is set, and only when **local** video generation is available.
- Clearing the start image clears the end image and resets strength to `0.8`.
- Advanced controls: “End frame strength” slider (`0.0–1.0`, default `0.8`).
- Wire through `use-generation` into the `generateVideo` body; omit end fields when no end image is set.
- Recovery / asset provenance: add optional `endImageUrl` and `endImageStrength` next to existing `inputImageUrl` on recovery context and `generationParams` (same persistence path as start image; omit when unused).
- A2V: if start + end + audio are all set, send all three; backend applies both image conditionings on the a2v path.

### Feature visibility

No new Settings toggle. Derive from existing “local video generate available.” Hide the end slot in API-only mode.

## Data flow

```
Start image set
  → End slot unlocks
  → Optional end image + strength
  → generateVideo({ imagePath, endImagePath?, endImageStrength?, audioPath? })
  → Validate (local-only, start required for end)
  → images = [start@0/1.0, end@last/strength?]
  → Local fast or a2v pipeline
  → Output video; record both image paths in asset metadata when present
```

## Error handling

| Case | Behavior |
| --- | --- |
| End without start | `400`; UI must not allow |
| Bad end path | Same as bad start `imagePath` |
| API-only + end set | UI hidden; server rejects if sent |
| Start cleared | Clear end + reset strength |
| Duration / frame count changes | Recompute last `frame_idx` at generate time from resolved `num_frames` |

## Testing

**Backend**

- Validation: end without start → 400; strength bounds; API-path rejection.
- Fake pipeline i2v: assert two conditionings `(0, 1.0)` and `(num_frames - 1, user_strength)`.
- Fake pipeline a2v: same dual conditioning with audio.
- Resize: end canvas matches start-derived size after preprocess.
- Regression: start-only i2v/a2v still one conditioning at frame 0.

**Frontend**

- End slot gated on start image + local mode; cleared with start.
- Payload includes end fields only when end is set.
- Recovery restores end image + strength.

**Manual**

- Local Gen Space: start+end at high/low strength; with and without audio; confirm last frame tracks end image at high strength.

## Success criteria

- User can run local i2v/a2v with optional last-frame conditioning from Gen Space without a new mode.
- End strength is adjustable; start remains fully pinned.
- API-only users never see a broken FLF control.
- Existing start-only generate paths keep current behavior and tests.
