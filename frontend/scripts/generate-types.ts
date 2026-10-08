import { readFileSync, writeFileSync } from 'node:fs'
import openapiTS, { astToString, type OpenAPI3 } from 'openapi-typescript'

export const GENERATED_HEADER = '// generated from openapi.json by `make api-types`, do not edit\n'

export async function renderTypes(schemaPath: string): Promise<string> {
  const schema = JSON.parse(readFileSync(schemaPath, 'utf-8')) as OpenAPI3
  const ast = await openapiTS(schema)
  return GENERATED_HEADER + astToString(ast)
}

// the output is written with `\n` whatever the host, so a Windows checkout never differs
async function main(): Promise<void> {
  const output = process.argv[2]
  const schemaPath = process.argv[3] ?? 'openapi.json'
  if (output === undefined) {
    throw new Error('usage: generate-types.ts <output file> [schema file]')
  }
  const text = await renderTypes(schemaPath)
  writeFileSync(output, text.replace(/\r\n/g, '\n'), 'utf-8')
}

if (import.meta.main) {
  await main()
}
