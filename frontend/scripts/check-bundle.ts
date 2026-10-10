import { readdirSync, readFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { gzipSync } from 'node:zlib'

export const INITIAL_JS_BUDGET_BYTES = 120 * 1024
export const EDITOR_JS_BUDGET_BYTES = 40 * 1024
export const DEV_SHIM_MARKER = '__PISNIAR_DEV_SHIM__'
// a line of the style wavesurfer writes into its shadow root: present wherever the library is
export const WAVESURFER_MARKER = ':host .noScrollbar'

const INLINE_SCRIPT = /<script\b(?![^>]*\bsrc=)[^>]*>/i
const INLINE_STYLE = /<style\b|\sstyle=/i
const INLINE_HANDLER = /\son[a-z]+\s*=/i
const DATA_ASSET = /\bdata:[a-z]+\/[a-z0-9.+-]+[;,]/i
const EVAL_CALL = /\beval\s*\(|\bnew\s+Function\b/
const LOCAL_ASSET = /(?:src|href)="(\/assets\/[^"]+\.js)"/g

export function htmlProblems(html: string): string[] {
  const problems: string[] = []
  if (INLINE_SCRIPT.test(html)) problems.push('index.html has an inline <script>')
  if (INLINE_STYLE.test(html)) problems.push('index.html has an inline style')
  if (INLINE_HANDLER.test(html)) problems.push('index.html has an inline event handler')
  if (DATA_ASSET.test(html)) problems.push('index.html has a data: asset')
  return problems
}

export function scriptProblems(name: string, source: string): string[] {
  const problems: string[] = []
  if (EVAL_CALL.test(source)) problems.push(`${name} uses eval or new Function`)
  if (source.includes(DEV_SHIM_MARKER)) problems.push(`${name} contains the dev shim marker`)
  return problems
}

export function styleProblems(name: string, source: string): string[] {
  return DATA_ASSET.test(source) ? [`${name} has a data: asset`] : []
}

// the entry script and its modulepreload links: what the first paint has to download
export function initialScriptPaths(html: string): string[] {
  return [...html.matchAll(LOCAL_ASSET)].flatMap((match) =>
    match[1] === undefined ? [] : [match[1]],
  )
}

export function gzipBytes(source: string | Buffer): number {
  return gzipSync(source).length
}

export interface BundleSizes {
  initialBytes: number
  // the chunks that contain wavesurfer and are not downloaded by the first paint
  editorBytes: number
  initialWithWavesurfer: string[]
  editorChunks: string[]
}

// the initial set is computed once; a script is wavesurfer's when it carries the marker
export function measureDist(distDirectory: string): BundleSizes {
  const html = readFileSync(join(distDirectory, 'index.html'), 'utf-8')
  const initial = new Set(initialScriptPaths(html).map((path) => basename(path)))
  const sizes: BundleSizes = {
    initialBytes: 0,
    editorBytes: 0,
    initialWithWavesurfer: [],
    editorChunks: [],
  }
  for (const name of readdirSync(join(distDirectory, 'assets'))) {
    if (!name.endsWith('.js')) continue
    const content = readFileSync(join(distDirectory, 'assets', name))
    const bytes = gzipBytes(content)
    const hasWavesurfer = content.toString('utf-8').includes(WAVESURFER_MARKER)
    if (initial.has(name)) {
      sizes.initialBytes += bytes
      if (hasWavesurfer) sizes.initialWithWavesurfer.push(name)
    } else if (hasWavesurfer) {
      sizes.editorBytes += bytes
      sizes.editorChunks.push(name)
    }
  }
  return sizes
}

export function sizeProblems(sizes: BundleSizes): string[] {
  const problems = sizes.initialWithWavesurfer.map(
    (name) => `${name} is an initial script and contains wavesurfer`,
  )
  if (sizes.editorChunks.length === 0) {
    problems.push('no lazy script contains wavesurfer: the editor or the marker is gone')
  }
  if (sizes.initialBytes > INITIAL_JS_BUDGET_BYTES) {
    problems.push(
      `initial JS is ${sizes.initialBytes} bytes gzip, the budget is ${INITIAL_JS_BUDGET_BYTES}`,
    )
  }
  if (sizes.editorBytes > EDITOR_JS_BUDGET_BYTES) {
    problems.push(
      `the wavesurfer chunk is ${sizes.editorBytes} bytes gzip, the budget is ${EDITOR_JS_BUDGET_BYTES}`,
    )
  }
  return problems
}

export function checkDist(distDirectory: string): string[] {
  const html = readFileSync(join(distDirectory, 'index.html'), 'utf-8')
  const problems = htmlProblems(html)

  const assetsDirectory = join(distDirectory, 'assets')
  for (const name of readdirSync(assetsDirectory)) {
    const source = readFileSync(join(assetsDirectory, name), 'utf-8')
    if (name.endsWith('.js')) problems.push(...scriptProblems(name, source))
    if (name.endsWith('.css')) problems.push(...styleProblems(name, source))
  }

  problems.push(...sizeProblems(measureDist(distDirectory)))
  return problems
}

function main(): void {
  const distDirectory = process.argv[2]
  if (distDirectory === undefined) {
    throw new Error('usage: check-bundle.ts <dist directory>')
  }
  const { initialBytes, editorBytes } = measureDist(distDirectory)
  console.log(
    `initial JS ${initialBytes} bytes gzip (budget ${INITIAL_JS_BUDGET_BYTES}), ` +
      `wavesurfer chunk ${editorBytes} bytes gzip (budget ${EDITOR_JS_BUDGET_BYTES})`,
  )
  const problems = checkDist(distDirectory)
  for (const problem of problems) console.error(problem)
  process.exitCode = problems.length === 0 ? 0 : 1
}

if (import.meta.main) {
  main()
}
