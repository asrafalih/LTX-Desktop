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
