import { describe, expect, it } from 'vitest'
import {
  DEFAULT_REGION_COLOR,
  FALLBACK_BUTTON_COLOR,
  FALLBACK_HINT_COLOR,
  clampToRules,
  formatTime,
  isWithinRules,
  nudge,
  regionColorOf,
  regionStateOf,
  snap,
  snapBounds,
  themeColorsOf,
  type Limits,
} from '../src/fragment/bounds'
import {
  FLAT_LEVEL,
  PEAK_MAX,
  flatChannelOf,
  peakChannelOf,
  waveformOf,
} from '../src/fragment/waveform'

const LIMITS: Limits = { durationMs: 200_000, maxLengthMs: 120_000 }

describe('snap', () => {
  it.each([
    [0, 0],
    [49, 0],
    [50, 100],
    [12_349, 12_300],
    [12_350, 12_400],
  ])('puts %i ms on the 100 ms grid as %i', (ms, expected) => {
    expect(snap(ms)).toBe(expected)
  })
})

describe('clampToRules', () => {
  it('leaves bounds that obey the rules alone', () => {
    expect(clampToRules({ startMs: 10_000, endMs: 55_000 }, LIMITS)).toEqual({
      startMs: 10_000,
      endMs: 55_000,
    })
  })

  it('rounds fractional milliseconds to whole ones', () => {
    expect(clampToRules({ startMs: 10_000.4, endMs: 55_000.6 }, LIMITS, 'end')).toEqual({
      startMs: 10_000,
      endMs: 55_001,
    })
  })

  it('keeps a moved region inside the track and keeps its length', () => {
    expect(clampToRules({ startMs: -5000, endMs: 40_000 }, LIMITS)).toEqual({
      startMs: 0,
      endMs: 45_000,
    })
    expect(clampToRules({ startMs: 180_000, endMs: 225_000 }, LIMITS)).toEqual({
      startMs: 155_000,
      endMs: 200_000,
    })
  })

  it('shortens a region that is longer than the maximum', () => {
    expect(clampToRules({ startMs: 0, endMs: 150_000 }, LIMITS)).toEqual({
      startMs: 0,
      endMs: 120_000,
    })
  })

  it('stops a dragged end at the maximum length and at the track end', () => {
    expect(clampToRules({ startMs: 10_000, endMs: 190_000 }, LIMITS, 'end').endMs).toBe(130_000)
    expect(clampToRules({ startMs: 100_000, endMs: 230_000 }, LIMITS, 'end').endMs).toBe(200_000)
  })

  it('stops a dragged start at the maximum length and at zero', () => {
    expect(clampToRules({ startMs: 10_000, endMs: 190_000 }, LIMITS, 'start').startMs).toBe(70_000)
    expect(clampToRules({ startMs: -400, endMs: 20_000 }, LIMITS, 'start').startMs).toBe(0)
  })

  it('never lets an edge cross the other one, and has no minimum length', () => {
    expect(clampToRules({ startMs: 30_000, endMs: 20_000 }, LIMITS, 'end')).toEqual({
      startMs: 30_000,
      endMs: 30_001,
    })
    expect(clampToRules({ startMs: 30_000, endMs: 20_000 }, LIMITS, 'start')).toEqual({
      startMs: 19_999,
      endMs: 20_000,
    })
  })

  it('takes a track shorter than the maximum whole', () => {
    const short: Limits = { durationMs: 30_000, maxLengthMs: 120_000 }

    expect(clampToRules({ startMs: 0, endMs: 90_000 }, short)).toEqual({
      startMs: 0,
      endMs: 30_000,
    })
  })

  it('always returns bounds the rule accepts', () => {
    for (const moved of ['start', 'end', 'both'] as const) {
      for (const start of [-10, 0, 1, 99_999, 199_999, 250_000]) {
        for (const end of [-10, 0, 1, 99_999, 200_000, 250_000]) {
          const result = clampToRules({ startMs: start, endMs: end }, LIMITS, moved)

          expect(isWithinRules(result, LIMITS)).toBe(true)
        }
      }
    }
  })
})

describe('isWithinRules', () => {
  it.each([
    [{ startMs: 0, endMs: 1 }, true],
    [{ startMs: 0, endMs: 120_000 }, true],
    [{ startMs: 80_000, endMs: 200_000 }, true],
    [{ startMs: 5, endMs: 5 }, false],
    [{ startMs: 6, endMs: 5 }, false],
    [{ startMs: -1, endMs: 10 }, false],
    [{ startMs: 0, endMs: 120_001 }, false],
    [{ startMs: 100_000, endMs: 200_001 }, false],
    [{ startMs: 0.5, endMs: 10 }, false],
  ])('judges %j as %s', (bounds, expected) => {
    expect(isWithinRules(bounds, LIMITS)).toBe(expected)
  })
})

describe('snapBounds', () => {
  it('snaps a resized edge to the grid', () => {
    expect(snapBounds({ startMs: 12_349, endMs: 57_412 }, LIMITS, 'end')).toEqual({
      startMs: 12_300,
      endMs: 57_400,
    })
  })

  it('moves a whole region by its snapped start and keeps the length', () => {
    expect(snapBounds({ startMs: 12_349, endMs: 57_349 }, LIMITS, 'both')).toEqual({
      startMs: 12_300,
      endMs: 57_300,
    })
  })

  it('keeps the very end of a track whose length is off the grid', () => {
    const odd: Limits = { durationMs: 215_342, maxLengthMs: 120_000 }

    expect(snapBounds({ startMs: 150_000, endMs: 215_330 }, odd, 'end').endMs).toBe(215_342)
  })

  it('still obeys the maximum after snapping', () => {
    expect(snapBounds({ startMs: 0, endMs: 119_999 }, LIMITS, 'end').endMs).toBe(120_000)
    expect(snapBounds({ startMs: 0, endMs: 120_049 }, LIMITS, 'end').endMs).toBe(120_000)
  })
})

describe('nudge', () => {
  const bounds = { startMs: 10_000, endMs: 55_000 }

  it('moves only the chosen edge, so the length changes', () => {
    expect(nudge(bounds, 'start', 1000, LIMITS)).toEqual({ startMs: 11_000, endMs: 55_000 })
    expect(nudge(bounds, 'end', -100, LIMITS)).toEqual({ startMs: 10_000, endMs: 54_900 })
  })

  it('stops the end at the maximum length without moving the start', () => {
    const long = { startMs: 10_000, endMs: 129_900 }

    expect(nudge(long, 'end', 1000, LIMITS)).toEqual({ startMs: 10_000, endMs: 130_000 })
  })

  it('stops the start at zero and at the end edge', () => {
    expect(nudge({ startMs: 400, endMs: 5000 }, 'start', -1000, LIMITS).startMs).toBe(0)
    expect(nudge({ startMs: 4000, endMs: 4500 }, 'start', 1000, LIMITS).startMs).toBe(4499)
  })

  it('stops the end at the track end', () => {
    expect(nudge({ startMs: 190_000, endMs: 199_950 }, 'end', 1000, LIMITS).endMs).toBe(200_000)
  })
})

describe('regionStateOf', () => {
  const bounds = { startMs: 12_300, endMs: 57_300 }

  it('puts the bounds back on the region in seconds and lets a ready editor touch it', () => {
    expect(regionStateOf(bounds, true)).toEqual({
      start: 12.3,
      end: 57.3,
      drag: true,
      resize: true,
    })
  })

  it('locks the region while the editor is not ready, yet still shows the bounds', () => {
    expect(regionStateOf(bounds, false)).toEqual({
      start: 12.3,
      end: 57.3,
      drag: false,
      resize: false,
    })
  })
})

describe('formatTime', () => {
  it.each([
    [0, '0:00.0'],
    [100, '0:00.1'],
    [59_999, '0:59.9'],
    [60_000, '1:00.0'],
    [83_400, '1:23.4'],
    [600_000, '10:00.0'],
    [-50, '0:00.0'],
    [Number.NaN, '0:00.0'],
  ])('writes %s ms as %s', (ms, expected) => {
    expect(formatTime(ms)).toBe(expected)
  })
})

describe('regionColorOf', () => {
  it('turns a hex colour into a translucent one', () => {
    expect(regionColorOf('#2481cc')).toBe('rgba(36, 129, 204, 0.3)')
    expect(regionColorOf(' #FFF ')).toBe('rgba(255, 255, 255, 0.3)')
  })

  it.each(['', 'red', 'rgb(1, 2, 3)', '#12', '#12345g'])('falls back for %j', (value) => {
    expect(regionColorOf(value)).toBe(DEFAULT_REGION_COLOR)
  })
})

describe('themeColorsOf', () => {
  it('reads the Telegram theme variables', () => {
    const variables: Record<string, string> = {
      '--tg-theme-hint-color': ' #999999 ',
      '--tg-theme-button-color': '#112233',
    }

    expect(themeColorsOf((name) => variables[name] ?? '')).toEqual({
      wave: '#999999',
      progress: '#112233',
      region: 'rgba(17, 34, 51, 0.3)',
    })
  })

  it('uses the page defaults outside Telegram', () => {
    expect(themeColorsOf(() => '')).toEqual({
      wave: FALLBACK_HINT_COLOR,
      progress: FALLBACK_BUTTON_COLOR,
      region: DEFAULT_REGION_COLOR,
    })
  })
})

describe('waveform', () => {
  it('scales the server peaks to 0..1 and clamps strays', () => {
    const channel = peakChannelOf([0, PEAK_MAX, 300, -4])

    expect([...channel]).toEqual([0, 1, 1, 0])
  })

  it('makes a flat line twenty values per second', () => {
    const channel = flatChannelOf(2500)

    expect(channel).toHaveLength(50)
    expect(channel.every((value) => value === Math.fround(FLAT_LEVEL))).toBe(true)
  })

  it('uses the peaks when the server sent them and a flat line when it did not', () => {
    const peaks = { peaks_per_second: 20, values: [10, 20] }

    expect(waveformOf({ peaks, audio_duration_ms: 1000 }).flat).toBe(false)
    expect(waveformOf({ peaks: null, audio_duration_ms: 1000 }).flat).toBe(true)
    expect(waveformOf({ peaks: { ...peaks, values: [] }, audio_duration_ms: 1000 }).flat).toBe(true)
  })
})
