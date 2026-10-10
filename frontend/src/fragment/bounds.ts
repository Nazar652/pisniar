// Pure rules for the fragment bounds the editor offers: whole milliseconds from the start of
// the full track, the same limits the server enforces, and the colours taken from Telegram's theme.

export const SNAP_MS = 100
export const COARSE_NUDGE_MS = 1000
export const FINE_NUDGE_MS = 100
export const DEFAULT_REGION_COLOR = 'rgba(36, 129, 204, 0.3)'
export const FALLBACK_HINT_COLOR = '#707579'
export const FALLBACK_BUTTON_COLOR = '#2481cc'
const REGION_ALPHA = 0.3
const HEX_COLOR = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i

export interface Bounds {
  startMs: number
  endMs: number
}

export interface Limits {
  durationMs: number
  maxLengthMs: number
}

export type Edge = 'start' | 'end'
// which part the user moved: one edge, or the whole region (its length is kept)
export type Moved = Edge | 'both'

export interface ThemeColors {
  wave: string
  progress: string
  region: string
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high)
}

export function snap(ms: number): number {
  return Math.round(ms / SNAP_MS) * SNAP_MS
}

// the rule: 0 <= start < end <= duration and end - start <= max; the moved part gives way, so
// dragging one edge never drags the other. An edge cannot cross the other, so no minimum exists.
export function clampToRules(bounds: Bounds, limits: Limits, moved: Moved = 'both'): Bounds {
  const duration = Math.max(1, Math.round(limits.durationMs))
  const maxLength = clamp(Math.round(limits.maxLengthMs), 1, duration)
  let start = Math.round(bounds.startMs)
  let end = Math.round(bounds.endMs)

  if (moved === 'start') {
    end = clamp(end, 1, duration)
    start = clamp(start, Math.max(0, end - maxLength), end - 1)
  } else if (moved === 'end') {
    start = clamp(start, 0, duration - 1)
    end = clamp(end, start + 1, Math.min(duration, start + maxLength))
  } else {
    const length = clamp(end - start, 1, maxLength)
    start = clamp(start, 0, duration - length)
    end = start + length
  }
  return { startMs: start, endMs: end }
}

export function isWithinRules(bounds: Bounds, limits: Limits): boolean {
  const { startMs, endMs } = bounds
  return (
    Number.isInteger(startMs) &&
    Number.isInteger(endMs) &&
    startMs >= 0 &&
    startMs < endMs &&
    endMs <= limits.durationMs &&
    endMs - startMs <= limits.maxLengthMs
  )
}

// a drop lands on the 100 ms grid; the end of the track stays reachable although it is off it
export function snapBounds(bounds: Bounds, limits: Limits, moved: Moved = 'both'): Bounds {
  const start = snap(bounds.startMs)
  const nearTrackEnd = bounds.endMs >= limits.durationMs - SNAP_MS / 2
  const end = nearTrackEnd ? limits.durationMs : snap(bounds.endMs)
  const next =
    moved === 'both'
      ? { startMs: start, endMs: start + bounds.endMs - bounds.startMs }
      : { startMs: start, endMs: end }
  return clampToRules(next, limits, moved)
}

// a step moves one edge and changes the length; at a limit the edge stops there
export function nudge(bounds: Bounds, edge: Edge, deltaMs: number, limits: Limits): Bounds {
  const moved =
    edge === 'start'
      ? { startMs: bounds.startMs + deltaMs, endMs: bounds.endMs }
      : { startMs: bounds.startMs, endMs: bounds.endMs + deltaMs }
  return clampToRules(moved, limits, edge)
}

export interface RegionState {
  start: number
  end: number
  drag: boolean
  resize: boolean
}

// what the waveform region must show: always the bounds (in seconds), and only a ready editor
// lets it be touched, so the region and the bounds cannot disagree
export function regionStateOf(bounds: Bounds, ready: boolean): RegionState {
  return { start: bounds.startMs / 1000, end: bounds.endMs / 1000, drag: ready, resize: ready }
}

// `m:ss.d`, truncated to a tenth so a value just below a minute never shows as 60 seconds
export function formatTime(ms: number): string {
  const tenths = Math.floor(Math.max(0, Number.isFinite(ms) ? ms : 0) / 100)
  const minutes = Math.floor(tenths / 600)
  const seconds = Math.floor(tenths / 10) % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}.${tenths % 10}`
}

export function regionColorOf(themeColor: string): string {
  const digits = HEX_COLOR.exec(themeColor.trim())?.[1]
  if (digits === undefined) return DEFAULT_REGION_COLOR
  const full = digits.length === 3 ? [...digits].map((digit) => digit + digit).join('') : digits
  const channel = (offset: number): number => parseInt(full.slice(offset, offset + 2), 16)
  return `rgba(${channel(0)}, ${channel(2)}, ${channel(4)}, ${REGION_ALPHA})`
}

// `read` returns the value of a CSS variable, empty when Telegram has not set it
export function themeColorsOf(read: (name: string) => string): ThemeColors {
  const hint = read('--tg-theme-hint-color').trim() || FALLBACK_HINT_COLOR
  const button = read('--tg-theme-button-color').trim() || FALLBACK_BUTTON_COLOR
  return { wave: hint, progress: button, region: regionColorOf(button) }
}
