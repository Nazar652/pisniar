import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { GENERATED_HEADER, renderTypes } from '../../scripts/generate-types.ts'

const SCHEMA = {
  openapi: '3.1.0',
  info: { title: 't', version: '1' },
  paths: {
    '/api/me': {
      get: {
        responses: {
          '200': {
            description: 'ok',
            content: {
              'application/json': {
                schema: { type: 'object', properties: { user_id: { type: 'integer' } } },
              },
            },
          },
        },
      },
    },
  },
}

const directories: string[] = []

afterEach(() => {
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true })
})

describe('renderTypes', () => {
  it('starts with the generated notice and describes the paths', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'schema-'))
    directories.push(directory)
    const file = join(directory, 'openapi.json')
    writeFileSync(file, JSON.stringify(SCHEMA))

    const text = await renderTypes(file)

    expect(text.startsWith(GENERATED_HEADER)).toBe(true)
    expect(text).toContain('"/api/me"')
    expect(text).toContain('user_id')
  })

  it('gives the same text for the same schema', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'schema-'))
    directories.push(directory)
    const file = join(directory, 'openapi.json')
    writeFileSync(file, JSON.stringify(SCHEMA))

    expect(await renderTypes(file)).toBe(await renderTypes(file))
  })
})
