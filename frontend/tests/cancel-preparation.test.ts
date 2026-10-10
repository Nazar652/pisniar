import { describe, expect, it, vi } from 'vitest'
import { CANCELLED_MESSAGE, useCancelPreparation } from '../src/fragment/useCancelPreparation'
import { errorResponse, fakeServer, jsonResponse } from './support'

const PATH = 'POST /api/posts/7/fragment/cancel'
const CANCELLED = { post_id: 7, status_label: 'Чернетка готова' }
const STAMP = '2026-05-01T12:00:00.123456Z'
const stamp = (): string => STAMP

describe('useCancelPreparation', () => {
  it('cancels, says the post is a draft again and tells the section', async () => {
    const server = fakeServer({ [PATH]: () => jsonResponse(CANCELLED) })
    const finished = vi.fn()
    const cancellation = useCancelPreparation(server.client, 7, stamp, finished)

    await cancellation.cancel()

    expect(server.callsTo(PATH)).toBe(1)
    expect(server.bodies).toEqual([{ expected_post_updated_at: STAMP }])
    expect(cancellation.message.value).toBe(CANCELLED_MESSAGE)
    expect(cancellation.failed.value).toBe(false)
    expect(cancellation.busy.value).toBe(false)
    expect(finished).toHaveBeenCalledExactlyOnceWith(true)
  })

  it('shows the server sentence as it came and keeps the section as it is', async () => {
    const server = fakeServer({
      [PATH]: () => errorResponse(409, 'post_busy', 'Пост зараз змінюється.'),
    })
    const finished = vi.fn()
    const cancellation = useCancelPreparation(server.client, 7, stamp, finished)

    await cancellation.cancel()

    expect(cancellation.failed.value).toBe(true)
    expect(cancellation.message.value).toBe('Пост зараз змінюється.')
    expect(finished).not.toHaveBeenCalled()
  })

  it('asks for a reload when the post is no longer prepared audio', async () => {
    const server = fakeServer({
      [PATH]: () => errorResponse(409, 'not_cancellable', 'Рендер уже почався.'),
    })
    const finished = vi.fn()
    const cancellation = useCancelPreparation(server.client, 7, stamp, finished)

    await cancellation.cancel()

    expect(cancellation.failed.value).toBe(true)
    expect(cancellation.message.value).toBe('Рендер уже почався.')
    expect(finished).toHaveBeenCalledExactlyOnceWith(false)
  })

  it('asks for a reload and shows the sentence when the post changed after it was read', async () => {
    const server = fakeServer({
      [PATH]: () => errorResponse(409, 'stale_data', 'Пост змінився, дані оновлено.'),
    })
    const finished = vi.fn()
    const cancellation = useCancelPreparation(server.client, 7, stamp, finished)

    await cancellation.cancel()

    expect(cancellation.failed.value).toBe(true)
    expect(cancellation.message.value).toBe('Пост змінився, дані оновлено.')
    expect(finished).toHaveBeenCalledExactlyOnceWith(false)
  })

  it('reads the stamp when it is pressed, not when it was created', async () => {
    const server = fakeServer({ [PATH]: () => jsonResponse(CANCELLED) })
    let current = 'first'
    const cancellation = useCancelPreparation(server.client, 7, () => current)
    current = 'second'

    await cancellation.cancel()

    expect(server.bodies).toEqual([{ expected_post_updated_at: 'second' }])
  })

  it('sends one request however often it is pressed while it waits', async () => {
    const server = fakeServer({ [PATH]: () => jsonResponse(CANCELLED) })
    const cancellation = useCancelPreparation(server.client, 7, stamp)

    const first = cancellation.cancel()
    expect(cancellation.busy.value).toBe(true)
    await Promise.all([first, cancellation.cancel()])

    expect(server.callsTo(PATH)).toBe(1)
  })

  it('reports a lost connection as a failure and can try again', async () => {
    let calls = 0
    const server = fakeServer({
      [PATH]: () => {
        calls += 1
        if (calls === 1) throw new TypeError('network')
        return jsonResponse(CANCELLED)
      },
    })
    const cancellation = useCancelPreparation(server.client, 7, stamp)

    await cancellation.cancel()
    expect(cancellation.failed.value).toBe(true)

    await cancellation.cancel()
    expect(cancellation.failed.value).toBe(false)
    expect(cancellation.message.value).toBe(CANCELLED_MESSAGE)
  })
})
