import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { WAVEFORM_HEIGHT_PX } from '../../src/fragment/waveform.ts'

const WAVESURFER_DIST = join(
  import.meta.dirname,
  '..',
  '..',
  'node_modules',
  'wavesurfer.js',
  'dist',
)
const APP_FILE = join(import.meta.dirname, '..', '..', '..', 'src', 'pisniar', 'webapp', 'app.py')

const STYLE_OPEN = '}>\\n        :host {'
const STYLE_CLOSE = '</style>'
const HEIGHT_EXPRESSION = '${this.getHeight(this.options.height,this.options.splitChannels)}'

function shipped(...path: string[]): string {
  return readFileSync(join(WAVESURFER_DIST, ...path), 'utf-8')
}

// the text of the `<style>` element wavesurfer writes into its shadow root, as the ES module
// ships it: newlines are escaped in the file and real ones in the string it builds
function shadowStyleText(heightPx: number): string {
  const source = shipped('wavesurfer.esm.js')
  const start = source.indexOf(STYLE_OPEN)
  const end = source.indexOf(STYLE_CLOSE, start)
  expect(start).toBeGreaterThan(-1)
  expect(end).toBeGreaterThan(start)
  return source
    .slice(start + 2, end)
    .replace(HEIGHT_EXPRESSION, String(heightPx))
    .replaceAll('\\n', '\n')
}

function hashOf(text: string): string {
  return `sha256-${createHash('sha256').update(text, 'utf-8').digest('base64')}`
}

describe('the CSP hash of the wavesurfer style', () => {
  it('is built from plain text only, so the hash is of what the browser parses', () => {
    const text = shadowStyleText(WAVEFORM_HEIGHT_PX)

    expect(text).not.toContain('\\')
    expect(text).not.toContain('${')
    expect(text).toContain(`min-height: ${WAVEFORM_HEIGHT_PX}px;`)
  })

  it('is the hash app.py allows at the fixed waveform height', () => {
    const app = readFileSync(APP_FILE, 'utf-8')
    const allowed = /WAVESURFER_STYLE_HASH = "(sha256-[A-Za-z0-9+/]+=*)"/.exec(app)?.[1]

    expect(hashOf(shadowStyleText(WAVEFORM_HEIGHT_PX))).toBe(allowed)
  })

  it('changes with the height, which is why the height is a constant', () => {
    expect(hashOf(shadowStyleText(WAVEFORM_HEIGHT_PX + 1))).not.toBe(
      hashOf(shadowStyleText(WAVEFORM_HEIGHT_PX)),
    )
  })

  it('writes no inline style attribute and no stylesheet object, so only the element needs a hash', () => {
    for (const code of [shipped('wavesurfer.esm.js'), shipped('plugins', 'regions.esm.js')]) {
      expect(code).not.toMatch(/style=|setAttribute\(["']style["']|cssText|adoptedStyleSheets/)
    }
  })
})
