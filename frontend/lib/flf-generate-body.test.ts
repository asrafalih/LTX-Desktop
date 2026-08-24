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
  it('uses explicit strength', () => {
    const body: Record<string, unknown> = {}
    applyFlfFields(body, '/a.png', '/b.png', 0.65)
    expect(body).toEqual({ endImagePath: '/b.png', endImageStrength: 0.65 })
  })
})
