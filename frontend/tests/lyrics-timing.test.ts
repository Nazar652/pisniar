import { describe, expect, it } from 'vitest'
import {
  MUSIC_MARK,
  NUDGE_GRID_MS,
  PREROLL_MS,
  SPEEDS,
  TAP_LATENCY_MS,
  bufferOf,
  compensatedMs,
  deleteLine,
  inFragment,
  insertLine,
  insertMusicMark,
  insertPause,
  linesFromPaste,
  linesLabel,
  nextUntimed,
  normalizedText,
  nudgeLine,
  retimeFrom,
  sameLines,
  seekBeforeMs,
  setText,
  slotsAt,
  tap,
  timedCount,
  undo,
  validate,
  type Line,
  type LyricsBuffer,
  type Patch,
  type Step,
  type TextLimits,
} from '../src/lyrics/timing'

const LIMITS: TextLimits = {
  maxLines: 300,
  maxLineLength: 500,
  maxTotalCharacters: 20_000,
  durationMs: 200_000,
}
const DURATION = LIMITS.durationMs

function line(text: string, startMs: number | null = null): Line {
  return { text, startMs }
}

function bufferWith(lines: Line[], cursor = 0): LyricsBuffer {
  return { lines, cursor }
}

function accepted(step: Step): { buffer: LyricsBuffer; patch: Patch } {
  if (!step.ok) throw new Error(`refused: ${step.message}`)
  return { buffer: step.buffer, patch: step.patch }
}

function refusal(step: Step): string {
  if (step.ok) throw new Error('accepted')
  return step.message
}

describe('constants', () => {
  it('keeps the tap latency in one named, non-negative number and the speeds the editor offers', () => {
    expect(Number.isFinite(TAP_LATENCY_MS)).toBe(true)
    expect(TAP_LATENCY_MS).toBeGreaterThanOrEqual(0)
    expect(SPEEDS).toEqual([1, 0.75, 0.5])
    expect(PREROLL_MS).toBe(3000)
  })

  it('compensates a tap by the latency times the playback rate, never below zero', () => {
    expect(compensatedMs(10_000, 1, 80)).toBe(9920)
    expect(compensatedMs(10_000, 0.5, 80)).toBe(9960)
    expect(compensatedMs(10_000, 0.75, 81)).toBe(9939)
    expect(compensatedMs(50, 1, 80)).toBe(0)
    expect(compensatedMs(10_000, 1, 0)).toBe(10_000)
    expect(compensatedMs(10_000, 1)).toBe(10_000 - TAP_LATENCY_MS)
    expect(seekBeforeMs(10_000)).toBe(7000)
    expect(seekBeforeMs(1000)).toBe(0)
  })
})

describe('tap', () => {
  it('times the line at the cursor and moves the cursor on', () => {
    const buffer = bufferWith([line('a'), line('b')])

    const { buffer: next } = accepted(tap(buffer, 1234, 1, DURATION))

    expect(next.lines).toEqual([line('a', compensatedMs(1234, 1)), line('b')])
    expect(next.cursor).toBe(1)
  })

  it('leaves the cursor past the last line when everything is timed, and refuses another tap', () => {
    const { buffer } = accepted(tap(bufferWith([line('a')]), 1000, 1, DURATION))

    expect(buffer.cursor).toBe(1)
    expect(refusal(tap(buffer, 2000, 1, DURATION))).toContain('вже відстукано')
  })

  it('refuses a tap that is not later than the previous timed line, and says so', () => {
    const buffer = bufferWith([line('a', 5000), line('b')], 1)

    expect(refusal(tap(buffer, 4000, 1, DURATION))).toContain('раніше за попередній рядок')
    expect(refusal(tap(buffer, 5000, 1, DURATION))).toContain('раніше за попередній рядок')
    expect(tap(buffer, 5001, 1, DURATION).ok).toBe(true)
  })

  it('looks past untimed lines for the previous timed one', () => {
    const buffer = bufferWith([line('a', 5000), line('b'), line('c')], 2)

    expect(tap(buffer, 4000, 1, DURATION).ok).toBe(false)
  })

  it('refuses a time at or after the end of the track', () => {
    const buffer = bufferWith([line('a')])

    expect(refusal(tap(buffer, DURATION, 1, DURATION))).toContain('поза треком')
    expect(tap(buffer, DURATION - 1, 1, DURATION).ok).toBe(true)
  })

  it('clears the later lines whose time is not above the tap and keeps the rest', () => {
    const buffer = bufferWith([
      line('a'),
      line('b', 3000),
      line('c'),
      line('d', 6000),
      line('e', 9000),
    ])

    const { buffer: next } = accepted(tap(buffer, 6000, 1, DURATION))

    expect(next.lines).toEqual([line('a', 6000), line('b'), line('c'), line('d'), line('e', 9000)])
  })

  it('drops a later pause that would lose its time, and undo brings it back', () => {
    const buffer = bufferWith([line('a'), line('', 3000), line('b', 9000)])

    const { buffer: next, patch } = accepted(tap(buffer, 4000, 1, DURATION))

    expect(next.lines).toEqual([line('a', 4000), line('b', 9000)])
    expect(undo(next, patch)).toEqual(buffer)
  })

  it('re-times a line that already has a time', () => {
    const buffer = bufferWith([line('a', 1000), line('b', 2000)], 0)

    const { buffer: next } = accepted(tap(buffer, 1500, 1, DURATION))

    expect(next.lines).toEqual([line('a', 1500), line('b', 2000)])
  })
})

describe('undo', () => {
  it('gives back exactly the buffer a tap changed, cleared lines and cursor included', () => {
    const buffer = bufferWith([line('a'), line('b', 3000), line('c', 4000), line('d', 8000)], 0)

    const { buffer: next, patch } = accepted(tap(buffer, 5000, 1, DURATION))

    expect(next).not.toEqual(buffer)
    expect(undo(next, patch)).toEqual(buffer)
  })

  it('takes back a nudge, a mark and a pause the same way', () => {
    const buffer = bufferWith([line('a', 1000), line('b', 5000), line('c')], 2)
    const nudged = accepted(nudgeLine(buffer, 0, 1000, DURATION))
    const marked = accepted(insertMusicMark(buffer, 3000, LIMITS))
    const paused = accepted(insertPause(buffer, 3000, LIMITS))

    expect(undo(nudged.buffer, nudged.patch)).toEqual(buffer)
    expect(undo(marked.buffer, marked.patch)).toEqual(buffer)
    expect(undo(paused.buffer, paused.patch)).toEqual(buffer)
  })
})

describe('nudgeLine', () => {
  it('moves a timed line by a step and keeps it on the 10 ms grid', () => {
    const buffer = bufferWith([line('a', 1234)])

    const { buffer: next } = accepted(nudgeLine(buffer, 0, 100, DURATION))

    expect(next.lines[0]?.startMs).toBe(1330)
    expect((next.lines[0]?.startMs ?? 1) % NUDGE_GRID_MS).toBe(0)
  })

  it('stays strictly between the neighbouring timed lines, skipping untimed ones', () => {
    const buffer = bufferWith([line('a', 1000), line('x'), line('b', 2000), line('c', 3000)])

    expect(refusal(nudgeLine(buffer, 2, 1000, DURATION))).toContain('між сусідніми')
    expect(refusal(nudgeLine(buffer, 2, -1000, DURATION))).toContain('між сусідніми')
    expect(nudgeLine(buffer, 2, 900, DURATION).ok).toBe(true)
    expect(nudgeLine(buffer, 2, 1000, DURATION).ok).toBe(false)
  })

  it('refuses a line without a time and a time outside the track', () => {
    expect(refusal(nudgeLine(bufferWith([line('a')]), 0, 100, DURATION))).toContain('немає часу')
    expect(nudgeLine(bufferWith([line('a', 50)]), 0, -100, DURATION).ok).toBe(false)
    expect(nudgeLine(bufferWith([line('a', DURATION - 50)]), 0, 100, DURATION).ok).toBe(false)
  })
})

describe('insertMusicMark and insertPause', () => {
  it('puts one line right after the last timed line before the time and shifts the cursor past it', () => {
    const buffer = bufferWith([line('a', 1000), line('b', 9000), line('c'), line('d')], 2)

    const { buffer: next } = accepted(insertMusicMark(buffer, 5000, LIMITS))

    expect(next.lines.map((entry) => entry.text)).toEqual(['a', MUSIC_MARK, 'b', 'c', 'd'])
    expect(next.lines[1]).toEqual(line(MUSIC_MARK, 5000))
    expect(next.cursor).toBe(3)
  })

  it('keeps the cursor on the same line when the mark goes after it', () => {
    const buffer = bufferWith([line('a', 1000), line('b')], 1)

    const { buffer: next } = accepted(insertMusicMark(buffer, 5000, LIMITS))

    expect(next.lines[next.cursor]?.text).toBe('b')
  })

  it('goes first when no timed line precedes it', () => {
    const buffer = bufferWith([line('a'), line('b', 9000)])

    const { buffer: next } = accepted(insertMusicMark(buffer, 500, LIMITS))

    expect(next.lines[0]).toEqual(line(MUSIC_MARK, 500))
  })

  it('refuses a time another line already has, a time outside the track and a full list', () => {
    const buffer = bufferWith([line('a', 1000)])

    expect(refusal(insertMusicMark(buffer, 1000, LIMITS))).toContain('уже є рядок')
    expect(refusal(insertMusicMark(buffer, DURATION, LIMITS))).toContain('поза треком')
    expect(refusal(insertMusicMark(buffer, 5, { ...LIMITS, maxLines: 1 }))).toContain('забагато')
  })

  it('inserts a pause as an empty line with a time', () => {
    const { buffer } = accepted(insertPause(bufferWith([line('a', 1000)]), 4000, LIMITS))

    expect(buffer.lines[1]).toEqual(line('', 4000))
  })
})

describe('structure edits', () => {
  it('sets a text, inserts and deletes lines and keeps the cursor on its line', () => {
    const buffer = bufferWith([line('a'), line('b'), line('c')], 1)

    expect(setText(buffer, 0, 'z').lines[0]?.text).toBe('z')
    expect(setText(buffer, 9, 'z')).toBe(buffer)
    const inserted = accepted(insertLine(buffer, 0, LIMITS)).buffer
    expect(inserted.lines).toHaveLength(4)
    expect(inserted.lines[inserted.cursor]?.text).toBe('b')
    const removed = accepted(deleteLine(buffer, 0)).buffer
    expect(removed.lines.map((entry) => entry.text)).toEqual(['b', 'c'])
    expect(removed.lines[removed.cursor]?.text).toBe('b')
    expect(refusal(insertLine(buffer, 0, { ...LIMITS, maxLines: 3 }))).toContain('забагато')
  })

  it('moves the cursor to a line to tap from there, inside the list', () => {
    const buffer = bufferWith([line('a'), line('b')])

    expect(retimeFrom(buffer, 1).cursor).toBe(1)
    expect(retimeFrom(buffer, 99).cursor).toBe(2)
    expect(retimeFrom(buffer, -3).cursor).toBe(0)
  })

  it('starts a loaded text at the first line without a time', () => {
    expect(bufferOf([line('a', 1), line('b')]).cursor).toBe(1)
    expect(bufferOf([line('a', 1)]).cursor).toBe(0)
    expect(timedCount([line('a', 1), line('b')])).toBe(1)
    expect(sameLines([line('a', 1)], [line('a', 1)])).toBe(true)
    expect(sameLines([line('a', 1)], [line('a', 2)])).toBe(false)
  })
})

describe('linesFromPaste', () => {
  it('makes one untimed line per non-empty row with the spaces collapsed', () => {
    const lines = linesFromPaste('  Перший   рядок \r\n\r\n\tДругий\nТретій  \r')

    expect(lines).toEqual([line('Перший рядок'), line('Другий'), line('Третій')])
  })

  it('makes nothing of blank text', () => {
    expect(linesFromPaste(' \n \r\n')).toEqual([])
  })
})

describe('slotsAt', () => {
  it('shows the line before, at and after the cursor and skips pauses on the sides', () => {
    const buffer = bufferWith(
      [line('a', 1), line('', 2), line('b', 3), line('c'), line('', 9), line('d')],
      3,
    )

    expect(slotsAt(buffer)).toEqual({ previous: 2, current: 3, next: 5 })
  })

  it('keeps a pause as the current line and has no current past the end', () => {
    expect(slotsAt(bufferWith([line('a', 1), line('', 2), line('b')], 1)).current).toBe(1)
    expect(slotsAt(bufferWith([line('a', 1)], 1))).toEqual({
      previous: 0,
      current: null,
      next: null,
    })
    expect(slotsAt(bufferWith([]))).toEqual({ previous: null, current: null, next: null })
  })
})

describe('inFragment', () => {
  it('marks a timed line inside the half-open fragment', () => {
    expect(inFragment(line('a', 1000), 1000, 5000)).toBe(true)
    expect(inFragment(line('a', 5000), 1000, 5000)).toBe(false)
    expect(inFragment(line('a', 999), 1000, 5000)).toBe(false)
    expect(inFragment(line('a'), 1000, 5000)).toBe(false)
  })
})

describe('linesLabel', () => {
  it.each([
    [1, '1 рядок'],
    [2, '2 рядки'],
    [5, '5 рядків'],
    [11, '11 рядків'],
    [21, '21 рядок'],
    [112, '112 рядків'],
  ])('says %i as %s', (count, text) => {
    expect(linesLabel(count)).toBe(text)
  })
})

describe('normalizedText', () => {
  // the characters Python's str.split() treats as spaces, and two it does not
  const SPACES = ['\u001c', '\u001d', '\u001e', '\u001f', '\u0085', ' ', ' ', '　']

  it.each(SPACES)('splits on U+%#', (space) => {
    expect(normalizedText(`а${space}${space}б`)).toBe('а б')
  })

  it('does not split on a zero-width no-break space or a zero-width space', () => {
    expect(normalizedText('а﻿б')).toBe('а﻿б')
    expect(normalizedText('а​б')).toBe('а​б')
  })

  it('counts the 500-character edge the way the server does', () => {
    const half = 'а'.repeat(249)
    for (const space of SPACES) {
      const text = `${half}${space}${space}${'б'.repeat(250)}`
      expect(Array.from(normalizedText(text))).toHaveLength(500)
      expect(validate([line(text)], LIMITS)).toBeNull()
      expect(validate([line(`${text}в`)], LIMITS)?.number).toBe(1)
    }
    // no separator is left to join, so the feff counts as a character of its own
    const glued = `${half}${String.fromCharCode(0xfeff)}${'б'.repeat(251)}`
    expect(validate([line(glued)], LIMITS)?.message).toContain('501 символів')
  })

  it('trims leading and trailing spaces', () => {
    expect(normalizedText('\u001f а  \u0085')).toBe('а')
    expect(normalizedText(' 　 ')).toBe('')
  })
})

describe('nextUntimed', () => {
  it('finds the next line after the cursor with no time, skipping timed ones', () => {
    const buffer = bufferWith([line('a'), line('b', 1), line('c'), line('d', 5), line('e')], 0)

    expect(nextUntimed(buffer)).toBe(2)
    expect(nextUntimed({ ...buffer, cursor: 2 })).toBe(4)
    expect(nextUntimed({ ...buffer, cursor: 4 })).toBeNull()
  })
})

describe('validate', () => {
  it('accepts a text with untimed lines, pauses and rising times', () => {
    expect(validate([line('a', 0), line('b'), line('', 500), line('c', 900)], LIMITS)).toBeNull()
    expect(validate([], LIMITS)).toBeNull()
  })

  it('names the first line that breaks a rule, with the server sentence', () => {
    expect(validate([line('a'), line('  ')], LIMITS)).toEqual({
      number: 2,
      message: 'Рядок 2 порожній і без часу — порожній рядок має бути паузою з часом.',
    })
    expect(validate([line('a', 5), line('b', 5)], LIMITS)?.message).toBe(
      'Час рядка 2 (5 мс) не пізніший за попередній відстуканий (5 мс).',
    )
    expect(validate([line('a', -1)], LIMITS)?.message).toContain('поза треком')
    expect(validate([line('a', DURATION)], LIMITS)?.message).toContain('поза треком')
    expect(validate([line('a', 1.5)], LIMITS)?.number).toBe(1)
  })

  it('counts characters after the spaces are collapsed, and the whole text', () => {
    const edge = `${'а'.repeat(249)}   ${'б'.repeat(250)}`
    expect(validate([line(`${'x'.repeat(500)}`)], LIMITS)).toBeNull()
    expect(validate([line(`${'x'.repeat(501)}`)], LIMITS)?.message).toContain('501 символів')
    expect(validate([line(edge)], LIMITS)).toBeNull()
    expect(validate([line(`${edge}ж`)], LIMITS)?.number).toBe(1)
    expect(validate([line(`${edge.slice(0, -1)}`)], LIMITS)).toBeNull()
    expect(validate([line('😀'.repeat(500))], LIMITS)).toBeNull()
    expect(validate([line('a'), line('b'), line('c')], { ...LIMITS, maxLines: 2 })?.number).toBe(3)
    expect(
      validate([line('abc'), line('def')], { ...LIMITS, maxTotalCharacters: 5 })?.message,
    ).toContain('з рядка 2')
  })
})

// a small deterministic generator, so a failing sequence can be replayed
function generator(seed: number): () => number {
  let state = seed
  return () => {
    state = (state + 0x6d2b79f5) | 0
    let t = Math.imul(state ^ (state >>> 15), 1 | state)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function strictlyRising(lines: readonly Line[]): boolean {
  const times = lines.flatMap((entry) => (entry.startMs === null ? [] : [entry.startMs]))
  return times.every((time, index) => index === 0 || time > (times[index - 1] ?? -1))
}

describe('properties', () => {
  it('keeps the timed lines rising when taps go on from an earlier line, over 1000 sequences', () => {
    for (let seed = 1; seed <= 1000; seed += 1) {
      const next = generator(seed)
      const count = 4 + Math.floor(next() * 10)
      let buffer = bufferOf(
        Array.from({ length: count }, (_, index) => line(`l${index}`, (index + 1) * 5000)),
      )

      for (let round = 0; round < 6; round += 1) {
        // «Відстукати звідси»: the cursor goes back, the audio runs from 3 s before that line
        buffer = retimeFrom(buffer, Math.floor(next() * buffer.lines.length))
        const from = buffer.lines[buffer.cursor]?.startMs ?? 0
        let position = seekBeforeMs(from) + Math.floor(next() * 4) * 500
        const taps = 1 + Math.floor(next() * 5)
        for (let index = 0; index < taps; index += 1) {
          position += Math.floor(next() * 40) * 100
          const step = tap(buffer, position, 1, DURATION)
          if (step.ok) buffer = step.buffer
          expect(strictlyRising(buffer.lines)).toBe(true)
          expect(validate(buffer.lines, LIMITS)).toBeNull()
          expect(buffer.cursor).toBeLessThanOrEqual(buffer.lines.length)
        }
      }
    }
  })

  it('keeps the timed lines strictly rising and takes every change back exactly, over 1000 sequences', () => {
    for (let seed = 1; seed <= 1000; seed += 1) {
      const next = generator(seed)
      const count = 3 + Math.floor(next() * 8)
      let buffer = bufferOf(Array.from({ length: count }, (_, index) => line(`l${index}`)))
      const stack: { before: LyricsBuffer; after: LyricsBuffer; patch: Patch }[] = []

      for (let operation = 0; operation < 25; operation += 1) {
        const pick = next()
        const position = Math.floor(next() * 60) * 1000 + Math.floor(next() * 10) * 100
        let step: Step | null = null
        if (pick < 0.5) step = tap(buffer, position, 1, DURATION)
        else if (pick < 0.65) step = insertMusicMark(buffer, position, LIMITS)
        else if (pick < 0.75) step = insertPause(buffer, position, LIMITS)
        else if (pick < 0.85) {
          step = nudgeLine(
            buffer,
            Math.floor(next() * buffer.lines.length),
            (next() - 0.5) * 4000,
            DURATION,
          )
        } else if (pick < 0.92)
          buffer = retimeFrom(buffer, Math.floor(next() * (buffer.lines.length + 1)))
        else if (stack.length > 0) {
          const last = stack.pop()
          if (last !== undefined) {
            buffer = undo(buffer, last.patch)
            expect(buffer).toEqual(last.before)
          }
        }
        if (step !== null && step.ok) {
          stack.push({ before: buffer, after: step.buffer, patch: step.patch })
          buffer = step.buffer
        }
        expect(strictlyRising(buffer.lines)).toBe(true)
        expect(buffer.cursor).toBeGreaterThanOrEqual(0)
        expect(buffer.cursor).toBeLessThanOrEqual(buffer.lines.length)
        expect(validate(buffer.lines, LIMITS)).toBeNull()
      }
      while (stack.length > 0) {
        const last = stack.pop()
        if (last === undefined) break
        expect(buffer.lines).toEqual(last.after.lines)
        buffer = undo(buffer, last.patch)
        expect(buffer).toEqual(last.before)
      }
    }
  })
})
