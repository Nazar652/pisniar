import { ESLint } from 'eslint'
import { describe, expect, it } from 'vitest'

async function ruleIdsFor(filePath: string, code: string): Promise<string[]> {
  const eslint = new ESLint()
  const [result] = await eslint.lintText(code, { filePath })
  return (result?.messages ?? []).map((message) => message.ruleId ?? 'no-rule-id')
}

describe('lint rules that keep dev code and unsafe markup out', () => {
  it('forbids a static import of src/dev outside the DEV guard', async () => {
    const ids = await ruleIdsFor('src/foo.ts', "import { x } from './dev/shim'\nexport { x }\n")

    expect(ids).toContain('no-restricted-imports')
  })

  it('forbids a dynamic import of src/dev outside src/main.ts', async () => {
    const ids = await ruleIdsFor('src/foo.ts', "export const load = () => import('./dev/shim')\n")

    expect(ids).toContain('no-restricted-syntax')
  })

  it('allows the guarded dynamic import in src/main.ts', async () => {
    const ids = await ruleIdsFor('src/main.ts', "export const load = () => import('./dev/shim')\n")

    expect(ids).toEqual([])
  })

  it('forbids v-html', async () => {
    const code =
      '<script setup lang="ts">\nconst html = "x"\n</script>\n<template><div v-html="html" /></template>\n'

    expect(await ruleIdsFor('src/Foo.vue', code)).toContain('vue/no-v-html')
  })

  it('forbids an explicit any', async () => {
    const ids = await ruleIdsFor('src/foo.ts', 'export const x: any = 1\n')

    expect(ids).toContain('@typescript-eslint/no-explicit-any')
  })

  it('forbids a ts-ignore comment', async () => {
    const ids = await ruleIdsFor('src/foo.ts', '// @ts-ignore\nexport const x: number = "a"\n')

    expect(ids).toContain('@typescript-eslint/ban-ts-comment')
  })

  it('reports an eslint-disable that suppresses nothing', async () => {
    const ids = await ruleIdsFor(
      'src/foo.ts',
      '// eslint-disable-next-line no-console\nexport const x = 1\n',
    )

    expect(ids).toContain('no-rule-id')
  })
})
