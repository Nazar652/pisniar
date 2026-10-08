import { describe, expect, it, vi } from 'vitest'
import {
  createApiClient,
  failureMessage,
  serverMessageOf,
  UNKNOWN_FAILURE_MESSAGE,
} from '../src/api/client'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('createApiClient', () => {
  it('sends the token as a bearer header', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ user_id: 5 })))
    const client = createApiClient({
      baseUrl: 'https://app.test',
      getToken: () => 'secret-token',
      fetch: fetchMock,
    })

    await client.GET('/api/me')

    const request = (fetchMock.mock.calls[0] as unknown as [Request])[0]
    expect(request.headers.get('Authorization')).toBe('Bearer secret-token')
    expect(request.url).toBe('https://app.test/api/me')
  })

  it('sends no header while there is no token', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ user_id: 5 })))
    const client = createApiClient({
      baseUrl: 'https://app.test',
      getToken: () => null,
      fetch: fetchMock,
    })

    await client.GET('/api/me')

    const request = (fetchMock.mock.calls[0] as unknown as [Request])[0]
    expect(request.headers.has('Authorization')).toBe(false)
  })

  it('reads the token again for every request', async () => {
    let token: string | null = null
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ user_id: 5 })))
    const client = createApiClient({
      baseUrl: 'https://app.test',
      getToken: () => token,
      fetch: fetchMock,
    })

    await client.GET('/api/me')
    token = 'later'
    await client.GET('/api/me')

    const second = (fetchMock.mock.calls[1] as unknown as [Request])[0]
    expect(second.headers.get('Authorization')).toBe('Bearer later')
  })
})

describe('serverMessageOf', () => {
  it('returns the message of an error body', () => {
    expect(serverMessageOf({ code: 'x', message: 'Речення.' })).toBe('Речення.')
  })

  it.each([null, undefined, 'text', 5, {}, { message: 5 }, { message: '' }])(
    'returns null for %j',
    (body) => {
      expect(serverMessageOf(body)).toBeNull()
    },
  )
})

describe('failureMessage', () => {
  it('prefers the sentence the server gave', () => {
    expect(failureMessage(409, { message: 'Власне речення.' })).toBe('Власне речення.')
  })

  it.each([401, 403, 413, 422, 503])('has its own sentence for %i', (status) => {
    expect(failureMessage(status, undefined)).not.toBe(UNKNOWN_FAILURE_MESSAGE)
  })

  it('falls back to a general sentence for any other status', () => {
    expect(failureMessage(418, undefined)).toBe(UNKNOWN_FAILURE_MESSAGE)
  })
})
