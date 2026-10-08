import { describe, expect, it, vi } from 'vitest'
import { NETWORK_FAILURE_MESSAGE } from '../src/api/client'
import { OPEN_FROM_TELEGRAM_MESSAGE, useSession } from '../src/session/useSession'

type Handler = (request: Request) => Response | Promise<Response>

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeServer(handlers: Record<string, Handler>) {
  const requests: Request[] = []
  const serve = vi.fn((input: Request) => {
    requests.push(input)
    const handler = handlers[`${input.method} ${new URL(input.url).pathname}`]
    return Promise.resolve(handler === undefined ? jsonResponse({}, 404) : handler(input))
  })
  return { serve: serve as unknown as typeof fetch, requests }
}

const TOKEN_REPLY = { token: 'session-token', expires_in: 3600 }

describe('useSession', () => {
  it('exchanges the raw initData for a token and asks who is signed in', async () => {
    const server = fakeServer({
      'POST /api/session': () => jsonResponse(TOKEN_REPLY),
      'GET /api/me': () => jsonResponse({ user_id: 111 }),
    })
    const session = useSession({
      baseUrl: 'https://app.test',
      initData: 'raw=data',
      fetch: server.serve,
    })

    await session.signIn()

    expect(session.state.value).toEqual({ kind: 'ready', userId: 111 })
    const [exchange, whoAmI] = server.requests
    expect(await exchange?.json()).toEqual({ init_data: 'raw=data' })
    expect(exchange?.headers.has('Authorization')).toBe(false)
    expect(whoAmI?.headers.get('Authorization')).toBe('Bearer session-token')
  })

  it('starts as loading', () => {
    const session = useSession({
      baseUrl: 'https://app.test',
      initData: 'x',
      fetch: fakeServer({}).serve,
    })

    expect(session.state.value).toEqual({ kind: 'loading' })
  })

  it('asks to open from Telegram when there is no initData and no token', async () => {
    const server = fakeServer({})
    const session = useSession({ baseUrl: 'https://app.test', initData: '', fetch: server.serve })

    await session.signIn()

    expect(session.state.value).toEqual({ kind: 'failed', message: OPEN_FROM_TELEGRAM_MESSAGE })
    expect(server.requests).toHaveLength(0)
  })

  it('shows the server sentence when the exchange is refused', async () => {
    const server = fakeServer({
      'POST /api/session': () =>
        jsonResponse({ code: 'forbidden', message: 'Доступ лише для адміністраторів.' }, 403),
    })
    const session = useSession({ baseUrl: 'https://app.test', initData: 'x', fetch: server.serve })

    await session.signIn()

    expect(session.state.value).toEqual({
      kind: 'failed',
      message: 'Доступ лише для адміністраторів.',
    })
    expect(server.requests).toHaveLength(1)
  })

  it('fails when the token is not accepted by the identity call', async () => {
    const server = fakeServer({
      'POST /api/session': () => jsonResponse(TOKEN_REPLY),
      'GET /api/me': () =>
        jsonResponse({ code: 'invalid_session', message: 'Сесія закінчилась.' }, 401),
    })
    const session = useSession({ baseUrl: 'https://app.test', initData: 'x', fetch: server.serve })

    await session.signIn()

    expect(session.state.value).toEqual({ kind: 'failed', message: 'Сесія закінчилась.' })
  })

  it('reports a network failure with its own sentence', async () => {
    const failing = vi.fn(() =>
      Promise.reject(new TypeError('Failed to fetch')),
    ) as unknown as typeof fetch
    const session = useSession({ baseUrl: 'https://app.test', initData: 'x', fetch: failing })

    await session.signIn()

    expect(session.state.value).toEqual({ kind: 'failed', message: NETWORK_FAILURE_MESSAGE })
  })

  it('skips the exchange when a token is already preset', async () => {
    const server = fakeServer({ 'GET /api/me': () => jsonResponse({ user_id: 7 }) })
    const session = useSession({
      baseUrl: 'https://app.test',
      initData: '',
      presetToken: 'dev-token',
      fetch: server.serve,
    })

    await session.signIn()

    expect(session.state.value).toEqual({ kind: 'ready', userId: 7 })
    expect(server.requests).toHaveLength(1)
    expect(server.requests[0]?.headers.get('Authorization')).toBe('Bearer dev-token')
  })

  it('keeps the token out of the page state', async () => {
    const server = fakeServer({
      'POST /api/session': () => jsonResponse(TOKEN_REPLY),
      'GET /api/me': () => jsonResponse({ user_id: 1 }),
    })
    const session = useSession({ baseUrl: 'https://app.test', initData: 'x', fetch: server.serve })

    await session.signIn()

    expect(JSON.stringify(session.state.value)).not.toContain('session-token')
  })
})
