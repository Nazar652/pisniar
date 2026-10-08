import { describe, expect, it, vi } from 'vitest'
import { startTelegram, telegramWebAppOf } from '../src/telegram/useTelegram'

function fakeWebApp(initData: string): TelegramWebApp {
  return { initData, ready: vi.fn(), expand: vi.fn() } as unknown as TelegramWebApp
}

describe('telegramWebAppOf', () => {
  it('returns the SDK object when Telegram provides one', () => {
    const webApp = fakeWebApp('x')

    expect(telegramWebAppOf({ Telegram: { WebApp: webApp } })).toBe(webApp)
  })

  it.each([{}, { Telegram: {} }])('returns undefined without an SDK in %j', (host) => {
    expect(telegramWebAppOf(host)).toBeUndefined()
  })
})

describe('startTelegram', () => {
  it('signals readiness, expands the page and returns the raw initData', () => {
    const webApp = fakeWebApp('query_id=1&hash=abc')

    expect(startTelegram(webApp)).toBe('query_id=1&hash=abc')
    expect(webApp.ready).toHaveBeenCalledOnce()
    expect(webApp.expand).toHaveBeenCalledOnce()
  })

  it('returns an empty string outside Telegram', () => {
    expect(startTelegram(undefined)).toBe('')
  })
})
