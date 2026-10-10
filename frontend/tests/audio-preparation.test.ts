import { effectScope, nextTick, reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  POLL_INTERVAL_MS,
  POLL_LIMIT_MS,
  PREPARATION_FAILED_MESSAGE,
  TIMED_OUT_MESSAGE,
  useAudioPreparation,
} from '../src/fragment/useAudioPreparation'
import { errorResponse, fakeServer, jsonResponse, makeDetail, type Handler } from './support'

type Fragment = ReturnType<typeof makeDetail>['fragment']

const PREPARING: Fragment = {
  start_ms: null,
  end_ms: null,
  choosable: false,
  preparable: false,
  preparing: true,
  cancellable: false,
  reason: 'Аудіо готується.',
}
const READY: Fragment = {
  start_ms: 0,
  end_ms: 45_000,
  choosable: true,
  preparable: false,
  preparing: false,
  cancellable: true,
  reason: null,
}
const FAILED: Fragment = {
  start_ms: null,
  end_ms: null,
  choosable: false,
  preparable: true,
  preparing: false,
  cancellable: false,
  reason: 'Минулу підготовку аудіо перервала помилка.',
}

const STARTED = { post_id: 7, status_label: 'Завантажується аудіо' }

// the post answers with the next fragment of the list, and with the last one from then on
function server(fragments: Fragment[], start: Handler = () => jsonResponse(STARTED)) {
  let read = 0
  return fakeServer({
    'POST /api/posts/7/fragment/audio': start,
    'GET /api/posts/7': () => {
      const fragment = fragments[Math.min(read, fragments.length - 1)] ?? READY
      read += 1
      return jsonResponse(makeDetail({ fragment }))
    },
  })
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useAudioPreparation', () => {
  it('asks for the preparation, then looks at the post every three seconds until it is ready', async () => {
    const fake = server([PREPARING, PREPARING, READY])
    const onReady = vi.fn()
    const preparation = useAudioPreparation(fake.client, 7, onReady)

    await preparation.start()

    expect(preparation.status.value).toBe('preparing')
    expect(fake.callsTo('POST /api/posts/7/fragment/audio')).toBe(1)
    expect(fake.callsTo('GET /api/posts/7')).toBe(0)

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS - 1)
    expect(fake.callsTo('GET /api/posts/7')).toBe(0)
    await vi.advanceTimersByTimeAsync(1)
    expect(fake.callsTo('GET /api/posts/7')).toBe(1)
    expect(preparation.status.value).toBe('preparing')

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    expect(preparation.status.value).toBe('preparing')
    expect(onReady).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    expect(preparation.status.value).toBe('ready')
    expect(onReady).toHaveBeenCalledOnce()

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5)
    expect(fake.callsTo('GET /api/posts/7')).toBe(3)
  })

  it('shows the server sentence of a refusal and does not poll', async () => {
    const fake = server([READY], () =>
      errorResponse(409, 'youtube_music_missing', 'У треку немає посилання YouTube Music.'),
    )
    const preparation = useAudioPreparation(fake.client, 7)

    await preparation.start()
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3)

    expect(preparation.status.value).toBe('failed')
    expect(preparation.message.value).toBe('У треку немає посилання YouTube Music.')
    expect(fake.callsTo('GET /api/posts/7')).toBe(0)
  })

  it('looks at the post at once when the server says the audio is already prepared', async () => {
    const fake = server([READY], () =>
      errorResponse(409, 'not_preparable', 'Аудіо вже підготовлено.'),
    )
    const onReady = vi.fn()
    const preparation = useAudioPreparation(fake.client, 7, onReady)

    await preparation.start()
    await vi.advanceTimersByTimeAsync(0)

    expect(preparation.status.value).toBe('ready')
    expect(onReady).toHaveBeenCalledOnce()
  })

  it('fails with the sentence of the post when the preparation ends in an error', async () => {
    const fake = server([PREPARING, FAILED])
    const preparation = useAudioPreparation(fake.client, 7)

    await preparation.start()
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2)

    expect(preparation.status.value).toBe('failed')
    expect(preparation.message.value).toBe('Минулу підготовку аудіо перервала помилка.')
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3)
    expect(fake.callsTo('GET /api/posts/7')).toBe(2)
  })

  it('stops neutrally, with no retry, when the post moved on without a failure', async () => {
    const moved: Fragment = { ...FAILED, preparable: false, reason: 'Зараз готується відео.' }
    const fake = server([PREPARING, moved])
    const preparation = useAudioPreparation(fake.client, 7)

    await preparation.start()
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2)

    expect(preparation.status.value).toBe('unavailable')
    expect(preparation.message.value).toBe('Зараз готується відео.')
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3)
    expect(fake.callsTo('GET /api/posts/7')).toBe(2)
  })

  it('has a sentence of its own when the post gives no reason', async () => {
    const fake = server([PREPARING, { ...FAILED, preparable: false, reason: null }])
    const preparation = useAudioPreparation(fake.client, 7)

    await preparation.start()
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2)

    expect(preparation.message.value).toBe(PREPARATION_FAILED_MESSAGE)
  })

  it('stops waiting after the limit and says so', async () => {
    const fake = server([PREPARING])
    const preparation = useAudioPreparation(fake.client, 7)

    await preparation.start()
    await vi.advanceTimersByTimeAsync(POLL_LIMIT_MS)

    expect(preparation.status.value).toBe('timed_out')
    expect(preparation.message.value).toBe(TIMED_OUT_MESSAGE)
    const reads = fake.callsTo('GET /api/posts/7')
    expect(reads).toBe(POLL_LIMIT_MS / POLL_INTERVAL_MS)
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5)
    expect(fake.callsTo('GET /api/posts/7')).toBe(reads)
  })

  it('treats failed reads as missed ticks and still ends at the limit', async () => {
    const fake = fakeServer({
      'POST /api/posts/7/fragment/audio': () => jsonResponse(STARTED),
      'GET /api/posts/7': () => errorResponse(503, 'database_unavailable', 'База недоступна.'),
    })
    const preparation = useAudioPreparation(fake.client, 7)

    await preparation.start()
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 4)
    expect(preparation.status.value).toBe('preparing')

    await vi.advanceTimersByTimeAsync(POLL_LIMIT_MS)
    expect(preparation.status.value).toBe('timed_out')
  })

  it('checks again from a timeout with a fresh limit', async () => {
    const fake = server([PREPARING])
    const preparation = useAudioPreparation(fake.client, 7)
    await preparation.start()
    await vi.advanceTimersByTimeAsync(POLL_LIMIT_MS)
    expect(preparation.status.value).toBe('timed_out')

    preparation.resume()

    expect(preparation.status.value).toBe('preparing')
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3)
    expect(preparation.status.value).toBe('preparing')
  })

  it('sends one request however often start is pressed while it waits', async () => {
    const fake = server([PREPARING])
    const preparation = useAudioPreparation(fake.client, 7)

    await Promise.all([preparation.start(), preparation.start()])
    await preparation.start()

    expect(fake.callsTo('POST /api/posts/7/fragment/audio')).toBe(1)
  })

  it('can be started again after a failure', async () => {
    let answers = 0
    const fake = server([READY], () =>
      answers++ === 0
        ? errorResponse(503, 'dispatch_failed', 'Черга недоступна.')
        : jsonResponse(STARTED),
    )
    const preparation = useAudioPreparation(fake.client, 7)

    await preparation.start()
    expect(preparation.status.value).toBe('failed')
    await preparation.start()

    expect(preparation.status.value).toBe('preparing')
    expect(preparation.message.value).toBe('')
  })

  it('starts polling when the post turns preparing after the mount, once', async () => {
    const fake = server([PREPARING])
    const preparation = useAudioPreparation(fake.client, 7)
    const post = reactive({ preparing: false })
    effectScope().run(() => preparation.followPreparing(() => post.preparing))

    post.preparing = true
    await nextTick()
    await vi.advanceTimersByTimeAsync(0)

    expect(preparation.status.value).toBe('preparing')
    expect(fake.callsTo('GET /api/posts/7')).toBe(1)

    post.preparing = false
    post.preparing = true
    await nextTick()
    expect(fake.callsTo('GET /api/posts/7')).toBe(1)
    preparation.dispose()
  })

  it('stops polling when disposed', async () => {
    const fake = server([PREPARING])
    const onReady = vi.fn()
    const preparation = useAudioPreparation(fake.client, 7, onReady)
    await preparation.start()

    preparation.dispose()
    await vi.advanceTimersByTimeAsync(POLL_LIMIT_MS)

    expect(fake.callsTo('GET /api/posts/7')).toBe(0)
    expect(onReady).not.toHaveBeenCalled()
  })

  it('does not start watching when disposed while the request was in flight', async () => {
    const fake = server([READY])
    const preparation = useAudioPreparation(fake.client, 7)

    const starting = preparation.start()
    preparation.dispose()
    await starting
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2)

    expect(fake.callsTo('GET /api/posts/7')).toBe(0)
  })
})
