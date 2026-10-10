import { describe, expect, it } from 'vitest'
import { shouldStopFragment } from '../src/fragment/playback'

describe('shouldStopFragment', () => {
  it('lets the fragment play until its end', () => {
    expect(shouldStopFragment('fragment', 45_000, 0)).toBe(false)
    expect(shouldStopFragment('fragment', 45_000, 44.99)).toBe(false)
  })

  it('stops at the end and anywhere past it', () => {
    expect(shouldStopFragment('fragment', 45_000, 45)).toBe(true)
    expect(shouldStopFragment('fragment', 45_000, 61.3)).toBe(true)
  })

  it('never stops the whole track or an idle player', () => {
    expect(shouldStopFragment('track', 45_000, 100)).toBe(false)
    expect(shouldStopFragment('idle', 45_000, 100)).toBe(false)
  })
})
