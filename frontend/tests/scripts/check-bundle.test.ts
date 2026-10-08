import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { randomBytes } from 'node:crypto'
import { afterEach, describe, expect, it } from 'vitest'
import {
  checkDist,
  DEV_SHIM_MARKER,
  gzipBytes,
  htmlProblems,
  INITIAL_JS_BUDGET_BYTES,
  initialScriptPaths,
  scriptProblems,
  styleProblems,
} from '../../scripts/check-bundle.ts'

const CLEAN_HTML = `<!doctype html><html><head>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script type="module" crossorigin src="/assets/index-abc.js"></script>
<link rel="modulepreload" crossorigin href="/assets/vendor-def.js">
<link rel="stylesheet" crossorigin href="/assets/index-abc.css">
</head><body><div id="app"></div></body></html>`

const directories: string[] = []

function makeDist(files: Record<string, string | Buffer>, html = CLEAN_HTML): string {
  const root = mkdtempSync(join(tmpdir(), 'dist-'))
  directories.push(root)
  mkdirSync(join(root, 'assets'))
  writeFileSync(join(root, 'index.html'), html)
  for (const [name, content] of Object.entries(files)) {
    writeFileSync(join(root, 'assets', name), content)
  }
  return root
}

afterEach(() => {
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true })
})

describe('htmlProblems', () => {
  it('accepts a page with only external and module scripts', () => {
    expect(htmlProblems(CLEAN_HTML)).toEqual([])
  })

  it.each([
    ['<script>alert(1)</script>', 'inline <script>'],
    ['<style>a{}</style>', 'inline style'],
    ['<div style="color:red">', 'inline style'],
    ['<button onclick="go()">', 'inline event handler'],
    ['<img src="data:image/png;base64,AAAA">', 'data: asset'],
  ])('flags %s', (fragment, expected) => {
    expect(htmlProblems(`<html>${fragment}</html>`).join(' ')).toContain(expected)
  })
})

describe('scriptProblems', () => {
  it('accepts ordinary code', () => {
    expect(scriptProblems('a.js', 'const x = evaluate(1); // no eval here')).toEqual([])
  })

  it.each(['eval("1")', 'new Function("return 1")', 'x = eval ("1")'])('flags %s', (code) => {
    expect(scriptProblems('a.js', code)).toHaveLength(1)
  })

  it('flags the dev shim marker', () => {
    expect(scriptProblems('a.js', `const m = "${DEV_SHIM_MARKER}"`)).toEqual([
      'a.js contains the dev shim marker',
    ])
  })
})

describe('styleProblems', () => {
  it('flags a data URL in a stylesheet', () => {
    expect(styleProblems('a.css', 'a{background:url(data:image/svg+xml;base64,AA)}')).toHaveLength(
      1,
    )
  })

  it('accepts a stylesheet that only references files', () => {
    expect(styleProblems('a.css', 'a{background:url(/assets/x.svg)}')).toEqual([])
  })
})

describe('initialScriptPaths', () => {
  it('lists the entry script and the preloaded chunks, not the stylesheet or the SDK', () => {
    expect(initialScriptPaths(CLEAN_HTML)).toEqual([
      '/assets/index-abc.js',
      '/assets/vendor-def.js',
    ])
  })
})

describe('gzipBytes', () => {
  it('is smaller than the input for repetitive text', () => {
    expect(gzipBytes('a'.repeat(5000))).toBeLessThan(200)
  })
})

describe('checkDist', () => {
  it('passes a clean small bundle', () => {
    const dist = makeDist({
      'index-abc.js': 'console.log(1)',
      'vendor-def.js': 'export {}',
      'index-abc.css': 'a{}',
    })

    expect(checkDist(dist)).toEqual([])
  })

  it('reports an eval in any script', () => {
    const dist = makeDist({ 'index-abc.js': 'eval("1")', 'vendor-def.js': 'export {}' })

    expect(checkDist(dist)).toEqual(['index-abc.js uses eval or new Function'])
  })

  it('reports an initial bundle over the budget', () => {
    const dist = makeDist({
      'index-abc.js': randomBytes(INITIAL_JS_BUDGET_BYTES + 5000),
      'vendor-def.js': '',
    })

    expect(checkDist(dist).join(' ')).toContain('the budget is')
  })
})
