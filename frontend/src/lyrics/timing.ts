// Pure rules of the lyrics buffer: lines with an optional time in whole milliseconds from the
// start of the full track, a cursor on the line to tap next, and the same limits the server holds.

import { formatTime } from '../fragment/bounds'

// how long a tap lags behind the sound, in audio milliseconds per unit of playback rate
export const TAP_LATENCY_MS = 0
export const PREROLL_MS = 3000
export const SPEEDS = [1, 0.75, 0.5] as const
export const NUDGE_GRID_MS = 10
export const FINE_NUDGE_MS = 100
export const COARSE_NUDGE_MS = 1000
export const MUSIC_MARK = '♪'
// the last entry of the text, with nothing after it, stays on the glass this long; `ass.LAST_LINE_MAX_MS` is the same number
export const LAST_LINE_MAX_MS = 8000
export const PAUSE_LABEL = '(програш)'
export const ALL_TAPPED_LABEL = 'Усі рядки відстукано'
const TAP_LABEL_PREFIX = 'Тап → '

export const NOTHING_TO_TAP_MESSAGE = 'Усі рядки вже відстукано.'
export const NOT_PLAYING_MESSAGE = 'Увімкніть відтворення, щоб відстукувати.'
export const NOT_TIMED_MESSAGE = 'У цього рядка ще немає часу.'
export const NO_ROOM_MESSAGE = 'Рядків забагато, нових додати не можна.'

const TAP_AFTER_END_MESSAGE = 'Час {time} поза треком: він триває лише {limit}.'
const TAP_TOO_EARLY_MESSAGE = 'Тап раніше за попередній рядок ({previous}). Відстукайте пізніше.'
const NUDGE_BLOCKED_MESSAGE = 'Так рядок зсунути не можна: він має лишатися між сусідніми.'
const MARK_SAME_TIME_MESSAGE = 'У цей момент уже є рядок ({time}). Виберіть інший.'
const MARK_OUT_OF_TRACK_MESSAGE = 'Час {time} поза треком: він триває лише {limit}.'

const TOO_MANY_LINES_MESSAGE =
  'Рядків забагато: дозволено не більше {limit}, а рядок {number} уже зайвий.'
const LINE_TOO_LONG_MESSAGE =
  'Рядок {number} задовгий: {length} символів, дозволено не більше {limit}.'
const EMPTY_UNTIMED_MESSAGE =
  'Рядок {number} порожній і без часу — порожній рядок має бути паузою з часом.'
const TIME_OUT_OF_TRACK_MESSAGE =
  'Час рядка {number} ({startMs} мс) поза треком: від 0 до {limit} мс.'
const TIME_NOT_RISING_MESSAGE =
  'Час рядка {number} ({startMs} мс) не пізніший за попередній відстуканий ({previousMs} мс).'
const TEXT_TOO_LONG_MESSAGE = 'Текст завеликий: разом з рядка {number} понад {limit} символів.'

export interface Line {
  text: string
  startMs: number | null
}

// `cursor` is the index of the line the next tap times; `lines.length` means every line is done
export interface LyricsBuffer {
  lines: readonly Line[]
  cursor: number
}

export interface TextLimits {
  maxLines: number
  maxLineLength: number
  maxTotalCharacters: number
  durationMs: number
}

// what one change replaced, enough to take it back exactly
export interface Patch {
  at: number
  removed: readonly Line[]
  inserted: number
  cursor: number
}

export type Step = { ok: true; buffer: LyricsBuffer; patch: Patch } | { ok: false; message: string }

export interface Problem {
  number: number
  message: string
}

export interface Slots {
  previous: number | null
  current: number | null
  next: number | null
}

function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, key: string) => String(values[key] ?? ''))
}

function refused(message: string): Step {
  return { ok: false, message }
}

// the characters Python's `str.split()` splits on (`str.isspace`): not U+FEFF, not U+200B
const SPACE_CODES = new Set([
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2028, 0x2029,
  0x202f, 0x205f, 0x3000,
])

function isSpace(char: string): boolean {
  const code = char.codePointAt(0) ?? 0
  return SPACE_CODES.has(code) || (code >= 0x2000 && code <= 0x200a)
}

export function normalizedText(text: string): string {
  const words: string[] = []
  let word = ''
  for (const char of text) {
    if (isSpace(char)) {
      if (word !== '') words.push(word)
      word = ''
    } else {
      word += char
    }
  }
  if (word !== '') words.push(word)
  return words.join(' ')
}

// the next line after the cursor that has no time yet
export function nextUntimed(buffer: LyricsBuffer): number | null {
  for (let i = buffer.cursor + 1; i < buffer.lines.length; i += 1) {
    if (buffer.lines[i]?.startMs === null) return i
  }
  return null
}

// the cursor starts on the first line without a time, past the end when every line has one
export function bufferOf(lines: readonly Line[]): LyricsBuffer {
  return { lines, cursor: nextUntimed({ lines, cursor: -1 }) ?? lines.length }
}

// what the tap button will time
export function tapLabel(buffer: LyricsBuffer): string {
  const line = buffer.lines[buffer.cursor]
  if (line === undefined) return ALL_TAPPED_LABEL
  return TAP_LABEL_PREFIX + (normalizedText(line.text) === '' ? PAUSE_LABEL : line.text)
}

export function timedCount(lines: readonly Line[]): number {
  return lines.filter((line) => line.startMs !== null).length
}

export function sameLines(left: readonly Line[], right: readonly Line[]): boolean {
  return (
    left.length === right.length &&
    left.every((line, index) => {
      const other = right[index]
      return other !== undefined && line.text === other.text && line.startMs === other.startMs
    })
  )
}

// 1 рядок, 2 рядки, 5 рядків
export function linesLabel(count: number): string {
  const lastTwo = count % 100
  const last = count % 10
  if (lastTwo >= 11 && lastTwo <= 14) return `${count} рядків`
  if (last === 1) return `${count} рядок`
  if (last >= 2 && last <= 4) return `${count} рядки`
  return `${count} рядків`
}

// the position of a tap is the sound heard a moment ago
export function compensatedMs(
  positionMs: number,
  rate: number,
  latencyMs: number = TAP_LATENCY_MS,
): number {
  return Math.max(0, Math.round(positionMs - latencyMs * rate))
}

export function seekBeforeMs(ms: number): number {
  return Math.max(0, ms - PREROLL_MS)
}

function spliced(
  buffer: LyricsBuffer,
  at: number,
  removeCount: number,
  insert: readonly Line[],
  cursor: number,
): Step {
  const lines = [...buffer.lines]
  const removed = lines.splice(at, removeCount, ...insert)
  return {
    ok: true,
    buffer: { lines, cursor: Math.min(Math.max(cursor, 0), lines.length) },
    patch: { at, removed, inserted: insert.length, cursor: buffer.cursor },
  }
}

// the exact inverse of the change that gave `buffer`
export function undo(buffer: LyricsBuffer, patch: Patch): LyricsBuffer {
  const lines = [...buffer.lines]
  lines.splice(patch.at, patch.inserted, ...patch.removed)
  return { lines, cursor: patch.cursor }
}

function timeBefore(lines: readonly Line[], index: number): number | null {
  for (let i = index - 1; i >= 0; i -= 1) {
    const startMs = lines[i]?.startMs
    if (startMs !== null && startMs !== undefined) return startMs
  }
  return null
}

function timeAfter(lines: readonly Line[], index: number): number | null {
  for (let i = index + 1; i < lines.length; i += 1) {
    const startMs = lines[i]?.startMs
    if (startMs !== null && startMs !== undefined) return startMs
  }
  return null
}

// the line at the cursor gets the time; later lines that would not rise above it lose theirs
export function tap(
  buffer: LyricsBuffer,
  positionMs: number,
  rate: number,
  durationMs: number,
): Step {
  const target = buffer.lines[buffer.cursor]
  if (target === undefined) return refused(NOTHING_TO_TAP_MESSAGE)
  const startMs = compensatedMs(positionMs, rate)
  if (startMs >= durationMs) {
    return refused(
      fill(TAP_AFTER_END_MESSAGE, { time: formatTime(startMs), limit: formatTime(durationMs) }),
    )
  }
  const previous = timeBefore(buffer.lines, buffer.cursor)
  if (previous !== null && startMs <= previous) {
    return refused(fill(TAP_TOO_EARLY_MESSAGE, { previous: formatTime(previous) }))
  }
  let last = buffer.cursor
  buffer.lines.forEach((line, index) => {
    if (index > buffer.cursor && line.startMs !== null && line.startMs <= startMs) last = index
  })
  // a pause that loses its time says nothing any more, so it goes
  const span = buffer.lines.slice(buffer.cursor, last + 1)
  const replacement = span.flatMap((line, offset): Line[] => {
    if (offset === 0) return [{ text: line.text, startMs }]
    if (line.startMs === null || line.startMs > startMs) return [line]
    return line.text === '' ? [] : [{ text: line.text, startMs: null }]
  })
  return spliced(buffer, buffer.cursor, span.length, replacement, buffer.cursor + 1)
}

// a tenth of a second or a second, on the 10 ms grid, between the neighbouring times
export function nudgeLine(
  buffer: LyricsBuffer,
  index: number,
  deltaMs: number,
  durationMs: number,
): Step {
  const line = buffer.lines[index]
  if (line === undefined || line.startMs === null) return refused(NOT_TIMED_MESSAGE)
  const startMs = Math.round((line.startMs + deltaMs) / NUDGE_GRID_MS) * NUDGE_GRID_MS
  const before = timeBefore(buffer.lines, index)
  const after = timeAfter(buffer.lines, index)
  const blocked =
    startMs < 0 ||
    startMs >= durationMs ||
    (before !== null && startMs <= before) ||
    (after !== null && startMs >= after)
  if (blocked) return refused(NUDGE_BLOCKED_MESSAGE)
  return spliced(buffer, index, 1, [{ text: line.text, startMs }], buffer.cursor)
}

export function retimeFrom(buffer: LyricsBuffer, index: number): LyricsBuffer {
  return { lines: buffer.lines, cursor: Math.min(Math.max(index, 0), buffer.lines.length) }
}

export function setText(buffer: LyricsBuffer, index: number, text: string): LyricsBuffer {
  const line = buffer.lines[index]
  if (line === undefined || line.text === text) return buffer
  const lines = [...buffer.lines]
  lines[index] = { text, startMs: line.startMs }
  return { lines, cursor: buffer.cursor }
}

export function insertLine(buffer: LyricsBuffer, at: number, limits: TextLimits): Step {
  if (buffer.lines.length >= limits.maxLines) return refused(NO_ROOM_MESSAGE)
  const position = Math.min(Math.max(at, 0), buffer.lines.length)
  const cursor = position <= buffer.cursor ? buffer.cursor + 1 : buffer.cursor
  return spliced(buffer, position, 0, [{ text: '', startMs: null }], cursor)
}

// a line with a time of its own goes right after the last timed line before it
function insertTimed(buffer: LyricsBuffer, ms: number, text: string, limits: TextLimits): Step {
  if (buffer.lines.length >= limits.maxLines) return refused(NO_ROOM_MESSAGE)
  if (ms < 0 || ms >= limits.durationMs) {
    return refused(
      fill(MARK_OUT_OF_TRACK_MESSAGE, {
        time: formatTime(ms),
        limit: formatTime(limits.durationMs),
      }),
    )
  }
  if (buffer.lines.some((line) => line.startMs === ms)) {
    return refused(fill(MARK_SAME_TIME_MESSAGE, { time: formatTime(ms) }))
  }
  let after = -1
  buffer.lines.forEach((line, index) => {
    if (line.startMs !== null && line.startMs < ms) after = index
  })
  const position = after + 1
  const cursor = position <= buffer.cursor ? buffer.cursor + 1 : buffer.cursor
  return spliced(buffer, position, 0, [{ text, startMs: ms }], cursor)
}

export function insertPause(buffer: LyricsBuffer, ms: number, limits: TextLimits): Step {
  return insertTimed(buffer, ms, '', limits)
}

export function insertMusicMark(buffer: LyricsBuffer, ms: number, limits: TextLimits): Step {
  return insertTimed(buffer, ms, MUSIC_MARK, limits)
}

export function deleteLine(buffer: LyricsBuffer, index: number): Step {
  if (buffer.lines[index] === undefined) return refused(NOT_TIMED_MESSAGE)
  return spliced(buffer, index, 1, [], index < buffer.cursor ? buffer.cursor - 1 : buffer.cursor)
}

// pasted text: one untimed line per non-empty row, spaces collapsed
export function linesFromPaste(text: string): Line[] {
  return text
    .split(/\r\n|\r|\n/)
    .map(normalizedText)
    .filter((row) => row !== '')
    .map((row) => ({ text: row, startMs: null }))
}

// `active` is the entry sounding at the position, a pause included; `current` is it only when it draws text
export interface Glass extends Slots {
  active: number | null
}

// What the video's glass shows at a track time, by `ass.display_window`: an entry lasts until the next
// timed one starts, the last one `LAST_LINE_MAX_MS`; a pause empties the middle slot; the sides are the nearest
// lines that draw something.
export function glassAt(lines: readonly Line[], positionMs: number): Glass {
  const timed: { index: number; startMs: number; spoken: boolean }[] = []
  lines.forEach((line, index) => {
    if (line.startMs !== null) {
      timed.push({ index, startMs: line.startMs, spoken: normalizedText(line.text) !== '' })
    }
  })
  const at = timed.findIndex((entry, order) => {
    const following = timed[order + 1]
    const endMs = following === undefined ? entry.startMs + LAST_LINE_MAX_MS : following.startMs
    return entry.startMs <= positionMs && positionMs < endMs
  })
  const entry = timed[at]
  if (entry === undefined) return { previous: null, current: null, next: null, active: null }
  const previous = timed
    .slice(0, at)
    .reverse()
    .find((other) => other.spoken)
  const next = timed.slice(at + 1).find((other) => other.spoken)
  return {
    previous: previous?.index ?? null,
    current: entry.spoken ? entry.index : null,
    next: next?.index ?? null,
    active: entry.index,
  }
}

// where a tap on a slot takes the audio: the line's own time, nothing for an empty slot or an untimed line
export function slotStartMs(lines: readonly Line[], index: number | null): number | null {
  return index === null ? null : (lines[index]?.startMs ?? null)
}

export function inFragment(line: Line, fragmentStartMs: number, fragmentEndMs: number): boolean {
  return line.startMs !== null && line.startMs >= fragmentStartMs && line.startMs < fragmentEndMs
}

// the server's rule for a saved text; the first line that breaks it is named by its number
export function validate(lines: readonly Line[], limits: TextLimits): Problem | null {
  let previousMs: number | null = null
  let total = 0
  for (const [index, line] of lines.entries()) {
    const number = index + 1
    const text = normalizedText(line.text)
    const fail = (message: string): Problem => ({ number, message })

    if (number > limits.maxLines) {
      return fail(fill(TOO_MANY_LINES_MESSAGE, { limit: limits.maxLines, number }))
    }
    // the server counts characters, not UTF-16 units
    const length = Array.from(text).length
    if (length > limits.maxLineLength) {
      return fail(fill(LINE_TOO_LONG_MESSAGE, { number, length, limit: limits.maxLineLength }))
    }
    if (text === '' && line.startMs === null) return fail(fill(EMPTY_UNTIMED_MESSAGE, { number }))

    if (line.startMs !== null) {
      if (
        !Number.isInteger(line.startMs) ||
        line.startMs < 0 ||
        line.startMs >= limits.durationMs
      ) {
        return fail(
          fill(TIME_OUT_OF_TRACK_MESSAGE, {
            number,
            startMs: line.startMs,
            limit: limits.durationMs,
          }),
        )
      }
      if (previousMs !== null && line.startMs <= previousMs) {
        return fail(fill(TIME_NOT_RISING_MESSAGE, { number, startMs: line.startMs, previousMs }))
      }
      previousMs = line.startMs
    }

    total += length
    if (total > limits.maxTotalCharacters) {
      return fail(fill(TEXT_TOO_LONG_MESSAGE, { number, limit: limits.maxTotalCharacters }))
    }
  }
  return null
}
