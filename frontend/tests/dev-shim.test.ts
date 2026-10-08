import { describe, expect, it, vi } from 'vitest'
import {
  createDevButton,
  DEV_SHIM_MARKER,
  DEV_TOKEN_KEY,
  installDevShim,
  type DevShimHost,
} from '../src/dev/shim'

function fakeHost(storedToken: string | null): DevShimHost {
  const element = {
    hidden: false,
    dataset: {} as Record<string, string>,
    textContent: '',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }
  return {
    sessionStorage: { getItem: (key: string) => (key === DEV_TOKEN_KEY ? storedToken : null) },
    document: {
      createElement: vi.fn(() => element),
      body: { append: vi.fn() },
    },
  } as unknown as DevShimHost
}

describe('installDevShim', () => {
  it('installs an SDK with an empty initData outside Telegram', () => {
    const host = fakeHost(null)

    installDevShim(host)

    expect(host.Telegram?.WebApp?.initData).toBe('')
    expect(host.Telegram?.WebApp?.isVersionAtLeast('99')).toBe(true)
  })

  it('returns the token a developer stored for the session', () => {
    expect(installDevShim(fakeHost('real-token'))).toBe('real-token')
  })

  it('returns nothing when no token is stored', () => {
    expect(installDevShim(fakeHost(null))).toBeUndefined()
  })

  it('leaves a real Telegram SDK alone', () => {
    const host = fakeHost(null)
    const realWebApp = { initData: 'real' } as unknown as TelegramWebApp
    host.Telegram = { WebApp: realWebApp }

    installDevShim(host)

    expect(host.Telegram.WebApp).toBe(realWebApp)
  })
})

describe('createDevButton', () => {
  it('starts hidden, is marked, and shows and hides on the same calls as Telegram', () => {
    const host = fakeHost(null)
    const button = createDevButton(host, 'OK')
    const element = vi.mocked(host.document.createElement).mock.results[0]?.value as {
      hidden: boolean
      dataset: Record<string, string>
    }

    expect(element.hidden).toBe(true)
    expect(element.dataset['devShim']).toBe(DEV_SHIM_MARKER)
    button.show()
    expect(element.hidden).toBe(false)
    button.hide()
    expect(element.hidden).toBe(true)
  })
})
