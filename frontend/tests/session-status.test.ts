import { renderToString } from 'vue/server-renderer'
import { createSSRApp } from 'vue'
import { describe, expect, it } from 'vitest'
import SessionStatus from '../src/components/SessionStatus.vue'
import type { SessionState } from '../src/session/useSession'

async function render(state: SessionState): Promise<string> {
  return renderToString(createSSRApp(SessionStatus, { state }))
}

describe('SessionStatus', () => {
  it('says the admin is signed in', async () => {
    expect(await render({ kind: 'ready', userId: 42 })).toContain('Ви увійшли як адмін (42)')
  })

  it('shows progress while loading', async () => {
    expect(await render({ kind: 'loading' })).toContain('Вхід…')
  })

  it('shows the failure sentence as an alert', async () => {
    const html = await render({ kind: 'failed', message: 'Сесія недійсна.' })

    expect(html).toContain('role="alert"')
    expect(html).toContain('Сесія недійсна.')
  })

  it('escapes a message instead of rendering it as markup', async () => {
    const html = await render({ kind: 'failed', message: '<img src=x onerror=alert(1)>' })

    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })
})
