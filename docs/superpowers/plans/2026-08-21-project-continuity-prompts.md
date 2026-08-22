# Project Continuity Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist per-project continuity entries + negative prompt, edit them in a project-header modal, and append continuity (plus send `negativePrompt`) on every in-project video generation path without changing the visible shot prompt.

**Architecture:** Optional `promptContinuity` on `ProjectV2` (Zod + local project JSON). Pure `composeProjectPrompt` helper is the single composition contract. Video generation hooks (`useGeneration`, `useRetake`, `useExtend`, `useIcLora`) read `activeProject.promptContinuity` and compose immediately before the API body so call sites keep storing/showing the user’s shot prompt only. UI is a draft-in-modal Save/Cancel flow on the project header.

**Tech Stack:** React 18, TypeScript, Zod 4, existing ProjectContext persistence, Vitest for the pure composer unit tests, Lucide icons, existing modal patterns (`fixed inset-0` overlay like `SettingsModal`).

**Global Constraints**

- Frontend-only compose; no backend schema/API changes for continuity.
- Image-only (`generateImage`) does not apply continuity (v1).
- UI / `generationParams.prompt` / asset prompts keep the shot prompt only; wire payload gets `finalPrompt`.
- Empty project `negativePrompt` → send `""` (backend default-negative fallback).
- Missing / undefined `promptContinuity` → passthrough user prompt + `negativePrompt: ""`.
- Do not rewrite the visible prompt textarea with the composed string.
- Keep Continuity separate from app-wide Settings.

---

### File map

| File | Responsibility |
|------|----------------|
| `frontend/types/project-model.ts` | Zod schemas + types for `promptContinuity` |
| `frontend/lib/compose-project-prompt.ts` | Pure compose + `hasActiveContinuity` |
| `frontend/lib/compose-project-prompt.test.ts` | Unit tests for compose |
| `vitest.config.ts` + `package.json` | Minimal frontend unit-test runner |
| `frontend/components/ProjectContinuityModal.tsx` | Draft modal UI |
| `frontend/views/Project.tsx` | Continuity button + modal wiring |
| `frontend/hooks/use-generation.ts` | Compose before `/api/generate` body |
| `frontend/hooks/use-retake.ts` | Compose before retake API |
| `frontend/hooks/use-extend.ts` | Compose before extend API |
| `frontend/hooks/use-ic-lora.ts` | Compose before IC-LoRA API |

Gap-fill in `VideoEditorTimelineEditingPanel` already calls `gapGenerationApi.generate(...)` → covered by `use-generation` once that hook composes.

---

### Task 1: Data model + composer + unit tests

**Files:**
- Modify: `frontend/types/project-model.ts`
- Create: `frontend/lib/compose-project-prompt.ts`
- Create: `frontend/lib/compose-project-prompt.test.ts`
- Create: `vitest.config.ts`
- Modify: `package.json`

**Interfaces:**
- Produces:
  - `PromptContinuityEntry`, `PromptContinuity`, optional `Project.promptContinuity`
  - `composeProjectPrompt(userPrompt: string, continuity?: PromptContinuity | null): { finalPrompt: string; negativePrompt: string }`
  - `hasActiveContinuity(continuity?: PromptContinuity | null): boolean`
  - `createPromptContinuityEntryId(): string`

- [ ] **Step 1: Add Vitest**

Add devDependency and script:

```json
"test:frontend": "vitest run"
```

Install:

```bash
pnpm add -D vitest
```

Create `vitest.config.ts`:

```ts
import path from 'path'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['frontend/**/*.test.ts'],
    environment: 'node',
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'frontend'),
    },
  },
})
```

- [ ] **Step 2: Write the failing composer tests**

Create `frontend/lib/compose-project-prompt.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import {
  composeProjectPrompt,
  hasActiveContinuity,
  type PromptContinuity,
} from './compose-project-prompt'

const continuity = (partial: Partial<PromptContinuity> = {}): PromptContinuity => ({
  entries: [],
  negativePrompt: '',
  ...partial,
})

describe('composeProjectPrompt', () => {
  it('passthrough when continuity missing', () => {
    expect(composeProjectPrompt('shot')).toEqual({
      finalPrompt: 'shot',
      negativePrompt: '',
    })
    expect(composeProjectPrompt('shot', null)).toEqual({
      finalPrompt: 'shot',
      negativePrompt: '',
    })
  })

  it('appends labeled entries in order after the shot prompt', () => {
    const result = composeProjectPrompt(
      'hero walks in',
      continuity({
        entries: [
          { id: '1', label: 'Character', text: 'red cloak' },
          { id: '2', label: 'Environment', text: 'moon base' },
        ],
      }),
    )
    expect(result.finalPrompt).toBe(
      'hero walks in\n\nCharacter: red cloak\n\nEnvironment: moon base',
    )
  })

  it('skips blank entry text and omits label prefix when label blank', () => {
    const result = composeProjectPrompt(
      'shot',
      continuity({
        entries: [
          { id: '1', label: 'Skip', text: '   ' },
          { id: '2', label: '  ', text: 'just text' },
        ],
      }),
    )
    expect(result.finalPrompt).toBe('shot\n\njust text')
  })

  it('passes through negativePrompt (including empty)', () => {
    expect(
      composeProjectPrompt('shot', continuity({ negativePrompt: 'blurry, watermark' }))
        .negativePrompt,
    ).toBe('blurry, watermark')
    expect(composeProjectPrompt('shot', continuity()).negativePrompt).toBe('')
  })
})

describe('hasActiveContinuity', () => {
  it('is true when any entry text or negative is non-empty', () => {
    expect(hasActiveContinuity(undefined)).toBe(false)
    expect(hasActiveContinuity(continuity())).toBe(false)
    expect(
      hasActiveContinuity(continuity({ entries: [{ id: '1', label: '', text: 'x' }] })),
    ).toBe(true)
    expect(hasActiveContinuity(continuity({ negativePrompt: 'n' }))).toBe(true)
  })
})
```

- [ ] **Step 3: Run tests — expect FAIL**

```bash
pnpm test:frontend
```

Expected: FAIL (module / symbols not found).

- [ ] **Step 4: Add Zod schemas on ProjectV2**

In `frontend/types/project-model.ts`, before `projectV2Schema`:

```ts
export const promptContinuityEntrySchema = z.object({
  id: z.string(),
  label: z.string(),
  text: z.string(),
})

export const promptContinuitySchema = z.object({
  entries: z.array(promptContinuityEntrySchema),
  negativePrompt: z.string(),
})
```

Extend `projectV2Schema`:

```ts
export const projectV2Schema = z.object({
  version: z.literal(2),
  id: z.string(),
  name: z.string(),
  createdAt: z.number(),
  updatedAt: z.number(),
  bins: assetBinsSchema,
  assets: z.array(assetSchema),
  timelines: z.array(timelineSchema),
  activeTimelineId: z.string().optional(),
  promptContinuity: promptContinuitySchema.optional(),
})
```

Export types:

```ts
export type PromptContinuityEntry = z.infer<typeof promptContinuityEntrySchema>
export type PromptContinuity = z.infer<typeof promptContinuitySchema>
```

Existing projects without the field keep parsing (optional).

- [ ] **Step 5: Implement composer**

Create `frontend/lib/compose-project-prompt.ts`:

```ts
import type { PromptContinuity } from '../types/project-model'

export type { PromptContinuity, PromptContinuityEntry } from '../types/project-model'

export function createPromptContinuityEntryId(): string {
  return `cont-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`
}

export function hasActiveContinuity(
  continuity?: PromptContinuity | null,
): boolean {
  if (!continuity) return false
  if (continuity.negativePrompt.trim()) return true
  return continuity.entries.some(entry => entry.text.trim().length > 0)
}

export function composeProjectPrompt(
  userPrompt: string,
  continuity?: PromptContinuity | null,
): { finalPrompt: string; negativePrompt: string } {
  if (!continuity) {
    return { finalPrompt: userPrompt, negativePrompt: '' }
  }

  let finalPrompt = userPrompt
  for (const entry of continuity.entries) {
    const text = entry.text.trim()
    if (!text) continue
    const label = entry.label.trim()
    finalPrompt += `\n\n${label ? `${label}: ${text}` : text}`
  }

  return {
    finalPrompt,
    negativePrompt: continuity.negativePrompt ?? '',
  }
}
```

- [ ] **Step 6: Run tests — expect PASS**

```bash
pnpm test:frontend
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add package.json pnpm-lock.yaml vitest.config.ts \
  frontend/types/project-model.ts \
  frontend/lib/compose-project-prompt.ts \
  frontend/lib/compose-project-prompt.test.ts
git commit -m "$(cat <<'EOF'
feat: add project promptContinuity model and composer

EOF
)"
```

---

### Task 2: ProjectContinuityModal + header entry point

**Files:**
- Create: `frontend/components/ProjectContinuityModal.tsx`
- Modify: `frontend/views/Project.tsx`

**Interfaces:**
- Consumes: `PromptContinuity`, `hasActiveContinuity`, `createPromptContinuityEntryId`, `setProject` / `activeProject`
- Produces: Modal props `{ isOpen, onClose, value, onSave }` where `onSave(next: PromptContinuity)` persists via parent

- [ ] **Step 1: Implement modal**

Create `frontend/components/ProjectContinuityModal.tsx` following `SettingsModal` overlay structure (`fixed inset-0 z-50`, backdrop, zinc-900 panel). Draft state resets when `isOpen` becomes true from `value`.

Key behavior:

```tsx
export interface ProjectContinuityModalProps {
  isOpen: boolean
  onClose: () => void
  value: PromptContinuity | undefined
  onSave: (next: PromptContinuity) => void
}

function emptyContinuity(): PromptContinuity {
  return { entries: [], negativePrompt: '' }
}

// On open: setDraft(structuredClone(value ?? emptyContinuity()))
// Add entry: { id: createPromptContinuityEntryId(), label: '', text: '' }
// Save: onSave(draft); onClose()
// Cancel / backdrop / X: onClose() without calling onSave
```

UI contents:

- Title: “Project continuity”
- For each entry: label `<input>`, multiline `<textarea>`, remove button (`Trash2`)
- “Add entry” button
- Negative prompt `<textarea>` at bottom with helper: “Used for all video generations in this project. Leave empty to use the app default.”
- Footer: Cancel + Save

Keep styling consistent with existing zinc / border tokens; no Settings tabs.

- [ ] **Step 2: Wire Project header**

In `frontend/views/Project.tsx`:

1. Replace the empty right spacer `<div className="flex-1" />` with a flex-1 container that right-aligns a Continuity control (keeps tabs centered):

```tsx
<div className="flex-1 flex items-center justify-end">
  <button
    type="button"
    onClick={() => setContinuityOpen(true)}
    className="relative flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-zinc-400 hover:text-white hover:bg-zinc-800 transition-colors"
    title="Project continuity"
  >
    <BookOpen className="h-4 w-4" />
    <span className="hidden sm:inline">Continuity</span>
    {hasActiveContinuity(activeProject.promptContinuity) ? (
      <span className="absolute top-1.5 right-1.5 h-1.5 w-1.5 rounded-full bg-blue-400" aria-hidden />
    ) : null}
  </button>
</div>
```

2. State: `const [continuityOpen, setContinuityOpen] = useState(false)`

3. Render:

```tsx
<ProjectContinuityModal
  isOpen={continuityOpen}
  onClose={() => setContinuityOpen(false)}
  value={activeProject.promptContinuity}
  onSave={(promptContinuity) => {
    setProject(activeProject.id, {
      ...activeProject,
      promptContinuity,
      updatedAt: Date.now(),
    })
  }}
/>
```

Import `BookOpen` from `lucide-react`.

- [ ] **Step 3: Typecheck**

```bash
pnpm typecheck:ts
```

Expected: PASS (no new errors in touched files).

- [ ] **Step 4: Commit**

```bash
git add frontend/components/ProjectContinuityModal.tsx frontend/views/Project.tsx
git commit -m "$(cat <<'EOF'
feat: add Continuity modal on project header

EOF
)"
```

---

### Task 3: Apply composer in video generation hooks

**Files:**
- Modify: `frontend/hooks/use-generation.ts`
- Modify: `frontend/hooks/use-retake.ts`
- Modify: `frontend/hooks/use-extend.ts`
- Modify: `frontend/hooks/use-ic-lora.ts`

**Interfaces:**
- Consumes: `composeProjectPrompt`, `useProjects().activeProject?.promptContinuity`
- Produces: API bodies use `finalPrompt`; `/api/generate` also sends composed `negativePrompt`. Call-site arguments remain the shot prompt (unchanged).

- [ ] **Step 1: `use-generation` — video only**

Import `useProjects` and `composeProjectPrompt`.

Inside `generate`, before building `body`:

```ts
const { finalPrompt, negativePrompt } = composeProjectPrompt(
  prompt,
  activeProject?.promptContinuity,
)
```

Use `finalPrompt` as `body.prompt` and `negativePrompt` as `body.negativePrompt` (replace the existing `(settings as { negativePrompt?: string }).negativePrompt ?? ''`).

Do **not** compose inside `generateImage`.

`activeProject` must be read from `useProjects()` at the top of the hook (same pattern as `useAppSettings`).

- [ ] **Step 2: `use-retake` / `use-extend` / `use-ic-lora`**

In each `submit*` callback, before the `ApiClient.*` call:

```ts
const { finalPrompt } = composeProjectPrompt(
  params.prompt,
  activeProject?.promptContinuity,
)
```

Pass `prompt: finalPrompt` in the API payload. Do not change refs / recovery / asset storage at GenSpace call sites (they still pass the shot `prompt`).

Retake/extend/ic-lora APIs that lack `negativePrompt` only get prompt append — that matches the design (“passed on every video generate as `negativePrompt`” for `/api/generate`; other video paths still get continuity text).

- [ ] **Step 3: Typecheck**

```bash
pnpm typecheck:ts
```

Expected: PASS.

- [ ] **Step 4: Re-run composer unit tests**

```bash
pnpm test:frontend
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/hooks/use-generation.ts frontend/hooks/use-retake.ts \
  frontend/hooks/use-extend.ts frontend/hooks/use-ic-lora.ts
git commit -m "$(cat <<'EOF'
feat: apply project continuity on video generate paths

EOF
)"
```

---

### Task 4: Manual verification checklist + design status

**Files:**
- Modify: `docs/superpowers/specs/2026-08-21-project-continuity-prompts-design.md` (status line only)

- [ ] **Step 1: Manual smoke (dev app)**

With `pnpm dev`:

1. Open a project → Continuity → add Character + Environment entries + a negative prompt → Save.
2. Confirm quiet indicator on the Continuity button.
3. GenSpace T2V: generate with a short shot prompt; in DevTools/network (or backend logs) confirm request `prompt` includes shot + `\n\nCharacter: …` and `negativePrompt` is the project value.
4. Confirm the GenSpace textarea still shows only the shot prompt after generate; new asset `generationParams.prompt` is the shot prompt only.
5. Timeline gap-fill video generate: same compose behavior.
6. One of retake / extend / IC-LoRA: confirm composed prompt on the request.
7. Cancel modal without Save: project continuity unchanged.
8. Clear all entry texts + negative → Save: indicator off; generate sends unchanged shot prompt and `negativePrompt: ""`.

- [ ] **Step 2: Update design status**

Change the design doc status from `Approved, pending implementation plan.` to `Implemented.`

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-08-21-project-continuity-prompts-design.md
git commit -m "$(cat <<'EOF'
docs: mark project continuity prompts as implemented

EOF
)"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|------------------|------|
| `promptContinuity` on ProjectV2 | Task 1 |
| `composeProjectPrompt` rules | Task 1 |
| Continuity header control + active indicator | Task 2 |
| Modal draft Save/Cancel | Task 2 |
| GenSpace / gap-fill via use-generation | Task 3 |
| use-extend / use-retake / use-ic-lora | Task 3 |
| Image-only out of scope | Task 3 (explicit skip) |
| Shot prompt unchanged in UI/storage | Task 3 (compose in hooks only) |
| Unit tests for composer | Task 1 |
| Manual verification | Task 4 |

## Placeholder / type consistency scan

- No TBD/TODO placeholders in steps.
- Types `PromptContinuity` / `composeProjectPrompt` / `hasActiveContinuity` / `createPromptContinuityEntryId` used consistently across tasks.
- Gap-fill covered via `use-generation` (no duplicate compose at panel).
