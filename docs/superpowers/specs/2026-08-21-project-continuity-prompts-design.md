# Project continuity prompts — design

**Date:** 2026-08-21  
**Status:** Approved, pending implementation plan.

## Problem

Shot prompts in a project often need the same character, environment, and style context every time. Today users retype that context into each prompt. Negative prompts can already be sent via `negativePrompt` on `/api/generate`, but there is no project-level UI or persistence for them, and empty values fall back to a backend default string.

## Goals

- Per-project continuity: a freeform list of named text entries (character bible, environment bible, and anything else).
- Per-project negative prompt, passed on every video generate as `negativePrompt`.
- Append continuity after the user’s shot prompt for all in-project video generation paths.
- Keep app-wide Settings (models, API keys, etc.) separate from project continuity.

## Non-goals (v1)

- App-wide continuity defaults.
- Per-generate “skip continuity” toggle.
- Applying continuity to image-only generation.
- Backend-owned project prompt storage or curl/API auto-merge.
- Hard character limits on bible text.
- Rewriting the visible prompt textarea with the composed string.

## Solution

Store continuity on the project document. Expose a project-header **Continuity** control that opens a modal. At send time, a shared frontend composer builds `finalPrompt` and `negativePrompt` for every video generation entry point.

### Data model

Add optional fields on `ProjectV2` (Zod schema in `frontend/types/project-model.ts`), persisted with the rest of the project:

```ts
promptContinuity: {
  entries: Array<{
    id: string
    label: string
    text: string
  }>
  negativePrompt: string
}
```

- Missing / undefined → treat as no entries and empty negative prompt.
- Entries with blank `text` (after trim) are skipped when composing.
- Blank `label` with non-empty `text` → append the text only (no `"Label: "` prefix).

### Composition

Single helper, e.g. `composeProjectPrompt(userPrompt, continuity)`:

```
finalPrompt =
  userPrompt
  + for each non-empty entry (in list order):
      "\n\n" + (label ? `${label}: ${text}` : text)

negativePrompt =
  continuity?.negativePrompt ?? ""
```

- Shot prompt stays first; continuity appends after.
- Empty `negativePrompt` → send `""` so the backend keeps today’s default-negative fallback.
- Non-empty project negative → send that string as `negativePrompt` (replaces the default for that request).

### Persistence vs wire payload

- UI prompt fields and stored `generationParams.prompt` / asset prompts keep the **user’s shot prompt** only.
- The composed `finalPrompt` is what is sent on the generate request.
- Regenerating an asset re-applies the **current** project continuity (not a snapshot from first generate).

## UI

### Entry point

In `frontend/views/Project.tsx` header, right side (currently an empty flex spacer): a **Continuity** button (icon + optional label / tooltip), distinct from the app-wide Settings gear in `App.tsx`.

Quiet indicator when continuity is “active”: at least one non-empty entry text, or a non-empty `negativePrompt`.

### Modal: `ProjectContinuityModal`

- Title: “Project continuity”
- Entries: list of rows — label input, multiline text, remove control
- **Add entry** (new id; empty label/text or label “Untitled”)
- **Negative prompt** textarea at the bottom, with helper copy: used for all video generations in this project; leave empty to use the app default
- Draft-in-modal with **Save** / **Cancel**. Save writes via existing project persist (`setProject` / `persistProject`). Cancel discards the draft without changing the project.

Out of scope in this modal: models, API keys, prompt enhancer, generation resolution/fps, etc.

## Integration

All in-project **video** generation paths call the composer before the API:

- `use-generation` (GenSpace T2V / I2V / A2V)
- Timeline / gap-fill generate paths that hit `/api/generate`
- `use-extend`
- `use-retake`
- `use-ic-lora`

No backend API or schema changes required: `/api/generate` already accepts `negativePrompt`.

Image-only (`generateImage`) is out of scope for v1.

### Edge cases

| Case | Behavior |
|------|----------|
| No active project / missing `promptContinuity` | Pass user prompt unchanged; `negativePrompt: ""` |
| Blank entry text | Skip entry |
| Blank label, non-empty text | Append text only |
| Very long bibles | Allowed (same as freeform prompts today) |
| Persist failure | Existing project save error handling |

## Testing

- Unit tests for `composeProjectPrompt`: append order, skip empty text, label formatting, empty continuity passthrough, negative prompt passthrough.
- No new backend tests required for composition.
- Manual: set entries + negative → generate from GenSpace and one timeline/extend/retake path → confirm request body has appended prompt and `negativePrompt`.

## Architecture notes

- **Why frontend compose (not backend):** project documents already live in frontend persistence; backend does not own project continuity today. Moving merge server-side would duplicate source of truth or require a larger project-sync effort.
- **Why shared helper (not ad-hoc per call site):** “apply to all video generations” fails silently if one path forgets to append; one composer is the contract.
- **Why separate from SettingsModal:** app Settings are global; continuity is story-specific and only meaningful with an open project.
