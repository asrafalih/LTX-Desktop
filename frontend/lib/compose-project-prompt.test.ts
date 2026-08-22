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
