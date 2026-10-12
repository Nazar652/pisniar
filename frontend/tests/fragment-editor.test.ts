import { describe, expect, it, vi } from 'vitest'
import {
  MEDIA_ERROR_MESSAGE,
  SUBMITTED_MESSAGE,
  useFragmentEditor,
} from '../src/fragment/useFragmentEditor'
import {
  errorResponse,
  fakeServer,
  jsonResponse,
  makeFragmentView,
  MICRO_STAMP,
  type FakeServer,
  type FragmentView,
  type Handler,
} from './support'

const GET = 'GET /api/posts/7/fragment'
const POST = 'POST /api/posts/7/fragment'
const STARTED = { post_id: 7, status_label: 'Рендериться', start_ms: 10_000, end_ms: 50_000 }

function serverWith(views: FragmentView[], extra: Record<string, Handler> = {}): FakeServer {
  let served = 0
  return fakeServer({
    [GET]: () => jsonResponse(views[Math.min(served++, views.length - 1)]),
    [POST]: () => jsonResponse(STARTED),
    ...extra,
  })
}

function lastBody(server: FakeServer): unknown {
  return server.bodies[server.requests.map((request) => request.method).lastIndexOf('POST')]
}

async function loaded(server: FakeServer) {
  const editor = useFragmentEditor(server.client, 7)
  await editor.load()
  return editor
}

describe('useFragmentEditor dirty', () => {
  it('is dirty only while the bounds differ from the server and nothing is submitted', async () => {
    const server = serverWith([makeFragmentView({ start_ms: 10_000, end_ms: 50_000 })])
    const editor = await loaded(server)
    expect(editor.dirty.value).toBe(false)

    editor.nudgeEdge('end', 1000)
    expect(editor.dirty.value).toBe(true)
    editor.nudgeEdge('end', -1000)
    expect(editor.dirty.value).toBe(false)

    editor.nudgeEdge('end', 1000)
    await editor.submit()
    expect(editor.status.value).toBe('submitted')
    expect(editor.dirty.value).toBe(false)
  })

  it('is clean before the view has loaded', () => {
    const editor = useFragmentEditor(serverWith([makeFragmentView()]).client, 7)

    expect(editor.dirty.value).toBe(false)
  })
})

describe('useFragmentEditor load', () => {
  it('starts loading and becomes ready with the bounds and the audio of the server', async () => {
    const server = serverWith([makeFragmentView({ start_ms: 5000, end_ms: 50_000 })])
    const editor = useFragmentEditor(server.client, 7)

    expect(editor.status.value).toBe('loading')
    await editor.load()

    expect(editor.status.value).toBe('ready')
    expect(editor.bounds.value).toEqual({ startMs: 5000, endMs: 50_000 })
    expect(editor.audioUrl.value).toBe('/api/posts/7/audio?token=first')
    expect(editor.limits.value).toEqual({ durationMs: 200_000, maxLengthMs: 120_000 })
  })

  it.each([
    ['not_choosable', 409, 'Пост зараз не можна змінити.'],
    ['audio_missing', 409, 'Аудіо ще не завантажено.'],
    ['post_not_found', 404, 'Пост не знайдено.'],
  ])('fails with the server sentence on %s', async (code, status, text) => {
    const server = fakeServer({ [GET]: () => errorResponse(status, code, text) })
    const editor = useFragmentEditor(server.client, 7)

    expect(await editor.load()).toBe(false)

    expect(editor.status.value).toBe('failed')
    expect(editor.message.value).toBe(text)
    expect(editor.view.value).toBeNull()
  })

  it('fails with the network sentence when the server cannot be reached', async () => {
    const server = fakeServer({
      [GET]: () => {
        throw new TypeError('offline')
      },
    })
    const editor = useFragmentEditor(server.client, 7)

    await editor.load()

    expect(editor.status.value).toBe('failed')
    expect(editor.message.value).toContain('Немає зв’язку')
  })
})

describe('useFragmentEditor bounds', () => {
  it('keeps a drag inside the rules and snaps the drop to the grid', async () => {
    const editor = await loaded(serverWith([makeFragmentView()]))

    editor.moveTo({ startMs: 12_349, endMs: 57_412 }, 'end', false)
    expect(editor.bounds.value).toEqual({ startMs: 12_349, endMs: 57_412 })
    editor.moveTo({ startMs: 12_349, endMs: 57_412 }, 'end', true)
    expect(editor.bounds.value).toEqual({ startMs: 12_300, endMs: 57_400 })
    editor.moveTo({ startMs: 0, endMs: 190_000 }, 'end', false)
    expect(editor.bounds.value).toEqual({ startMs: 0, endMs: 120_000 })
  })

  it('moves one edge by a step and stops at the limits', async () => {
    const editor = await loaded(serverWith([makeFragmentView()]))

    editor.nudgeEdge('start', 1000)
    editor.nudgeEdge('end', -100)
    expect(editor.bounds.value).toEqual({ startMs: 1000, endMs: 44_900 })
    editor.nudgeEdge('start', -5000)
    expect(editor.bounds.value.startMs).toBe(0)
  })

  it('ignores moves before the view is loaded', () => {
    const editor = useFragmentEditor(serverWith([makeFragmentView()]).client, 7)

    editor.moveTo({ startMs: 5000, endMs: 9000 }, 'both', true)
    editor.nudgeEdge('end', 1000)

    expect(editor.bounds.value).toEqual({ startMs: 0, endMs: 1 })
  })

  it('drops moves and nudges while a submission is running or done, and keeps the bounds', async () => {
    let release: (response: Response) => void = () => undefined
    const answer = new Promise<Response>((resolve) => (release = resolve))
    const server = serverWith([makeFragmentView()], { [POST]: () => answer })
    const editor = await loaded(server)

    const pending = editor.submit()
    editor.moveTo({ startMs: 30_000, endMs: 70_000 }, 'both', true)
    editor.nudgeEdge('end', 1000)

    expect(editor.status.value).toBe('submitting')
    expect(editor.bounds.value).toEqual({ startMs: 0, endMs: 45_000 })
    release(errorResponse(409, 'post_busy', 'Зайнято.'))
    await pending
    expect(editor.status.value).toBe('ready')
    expect(editor.bounds.value).toEqual({ startMs: 0, endMs: 45_000 })
  })

  it('sets an edge at the playhead on the grid, and the edge set wins', async () => {
    const editor = await loaded(serverWith([makeFragmentView()]))

    editor.setEdgeAt('start', 12.349)
    expect(editor.bounds.value).toEqual({ startMs: 12_300, endMs: 45_000 })
    editor.setEdgeAt('end', 70.02)
    expect(editor.bounds.value).toEqual({ startMs: 12_300, endMs: 70_000 })
    editor.setEdgeAt('end', 150)
    expect(editor.bounds.value).toEqual({ startMs: 30_000, endMs: 150_000 })
  })

  it('keeps the length when an edge is set across the other one', async () => {
    const editor = await loaded(serverWith([makeFragmentView()]))

    editor.setEdgeAt('start', 60)

    expect(editor.bounds.value).toEqual({ startMs: 60_000, endMs: 105_000 })
  })

  it('does not set an edge from the playhead before ready or while submitting', async () => {
    const early = useFragmentEditor(serverWith([makeFragmentView()]).client, 7)
    early.setEdgeAt('start', 10)
    expect(early.bounds.value).toEqual({ startMs: 0, endMs: 1 })

    const answer = new Promise<Response>(() => undefined)
    const editor = await loaded(serverWith([makeFragmentView()], { [POST]: () => answer }))
    void editor.submit()
    editor.setEdgeAt('start', 10)

    expect(editor.bounds.value).toEqual({ startMs: 0, endMs: 45_000 })
  })

  it('calls back once the render has been accepted, and not on a refusal', async () => {
    const accepted = vi.fn()
    const refused = vi.fn()
    const ok = serverWith([makeFragmentView()])
    const bad = serverWith([makeFragmentView()], {
      [POST]: () => errorResponse(409, 'post_busy', 'Зайнято.'),
    })
    const first = useFragmentEditor(ok.client, 7, accepted)
    const second = useFragmentEditor(bad.client, 7, refused)
    await first.load()
    await second.load()

    await first.submit()
    await second.submit()

    expect(accepted).toHaveBeenCalledOnce()
    expect(refused).not.toHaveBeenCalled()
  })

  it('allows a submission only when ready with bounds that obey the rules', async () => {
    const editor = useFragmentEditor(serverWith([makeFragmentView()]).client, 7)
    expect(editor.canSubmit.value).toBe(false)

    await editor.load()

    expect(editor.canSubmit.value).toBe(true)
  })

  it('submits with the stamp the post was last read with, keeping the dragged bounds', async () => {
    const server = serverWith([makeFragmentView({ post_updated_at: MICRO_STAMP })])
    const editor = await loaded(server)
    editor.nudgeEdge('end', 1000)

    editor.adoptStamp('2026-10-10T08:00:00.000001Z')
    await editor.submit()

    expect(lastBody(server)).toMatchObject({
      end_ms: 46_000,
      expected_post_updated_at: '2026-10-10T08:00:00.000001Z',
    })
  })

  it('ignores a stamp before the view has loaded', async () => {
    const server = serverWith([makeFragmentView({ post_updated_at: MICRO_STAMP })])
    const editor = useFragmentEditor(server.client, 7)

    editor.adoptStamp('2026-10-10T08:00:00.000001Z')
    await editor.load()
    await editor.submit()

    expect(lastBody(server)).toMatchObject({
      expected_post_updated_at: '2026-10-09T12:34:56.123456Z',
    })
  })
})

describe('useFragmentEditor submit', () => {
  it('sends the bounds and the stamp exactly as the server gave it, microseconds included', async () => {
    const server = serverWith([makeFragmentView({ post_updated_at: MICRO_STAMP })])
    const editor = await loaded(server)
    editor.nudgeEdge('start', 1000)

    await editor.submit()

    expect(server.callsTo(POST)).toBe(1)
    expect(lastBody(server)).toEqual({
      start_ms: 1000,
      end_ms: 45_000,
      expected_post_updated_at: '2026-10-09T12:34:56.123456Z',
    })
    expect(editor.status.value).toBe('submitted')
    expect(editor.message.value).toBe(SUBMITTED_MESSAGE)
  })

  it('keeps an offset stamp as it came too', async () => {
    const server = serverWith([
      makeFragmentView({ post_updated_at: '2026-10-09T15:34:56.000001+03:00' }),
    ])
    const editor = await loaded(server)

    await editor.submit()

    expect(lastBody(server)).toMatchObject({
      expected_post_updated_at: '2026-10-09T15:34:56.000001+03:00',
    })
  })

  it('does not send a second request once submitted or while submitting', async () => {
    const server = serverWith([makeFragmentView()])
    const editor = await loaded(server)

    const first = editor.submit()
    await editor.submit()
    await first
    await editor.submit()

    expect(server.callsTo(POST)).toBe(1)
  })

  it('reloads the view on stale_data and shows the sentence with the new bounds', async () => {
    const fresh = makeFragmentView({
      post_updated_at: '2026-10-09T13:00:00.000002Z',
      start_ms: 20_000,
      end_ms: 60_000,
    })
    const server = serverWith([makeFragmentView(), fresh], {
      [POST]: () => errorResponse(409, 'stale_data', 'Пост змінився. Дані оновлено.'),
    })
    const editor = await loaded(server)

    await editor.submit()

    expect(server.callsTo(GET)).toBe(2)
    expect(editor.status.value).toBe('ready')
    expect(editor.bounds.value).toEqual({ startMs: 20_000, endMs: 60_000 })
    expect(editor.message.value).toBe('Пост змінився. Дані оновлено.')

    await editor.submit()
    expect(lastBody(server)).toMatchObject({
      expected_post_updated_at: '2026-10-09T13:00:00.000002Z',
    })
  })

  it('shows the failure of a reload after stale_data instead of the stale sentence', async () => {
    let served = 0
    const server = fakeServer({
      [GET]: () =>
        served++ === 0
          ? jsonResponse(makeFragmentView())
          : errorResponse(409, 'not_choosable', 'Пост уже опубліковано.'),
      [POST]: () => errorResponse(409, 'stale_data', 'Пост змінився.'),
    })
    const editor = await loaded(server)

    await editor.submit()

    expect(editor.status.value).toBe('failed')
    expect(editor.message.value).toBe('Пост уже опубліковано.')
  })

  it.each([
    ['invalid_fragment', 422, 'Фрагмент не підходить: довший за 2:00.', 'ready'],
    ['post_busy', 409, 'Пост зайнятий, спробуйте за мить.', 'ready'],
    ['queue_unavailable', 503, 'Черга недоступна.', 'ready'],
    ['not_choosable', 409, 'Пост зараз не можна змінити.', 'failed'],
    ['audio_missing', 409, 'Аудіо ще не завантажено.', 'failed'],
    ['dispatch_failed', 503, 'Не вдалося поставити рендер. Надішліть /retry 7.', 'failed'],
  ])('shows the server sentence of %s and ends in %s', async (code, httpStatus, text, expected) => {
    const server = serverWith([makeFragmentView()], {
      [POST]: () => errorResponse(httpStatus, code, text),
    })
    const editor = await loaded(server)

    await editor.submit()

    expect(editor.message.value).toBe(text)
    expect(editor.status.value).toBe(expected)
  })

  it('lets the admin try again after a refusal that leaves the editor open', async () => {
    let attempts = 0
    const server = serverWith([makeFragmentView()], {
      [POST]: () =>
        attempts++ === 0 ? errorResponse(409, 'post_busy', 'Зайнято.') : jsonResponse(STARTED),
    })
    const editor = await loaded(server)

    await editor.submit()
    await editor.submit()

    expect(editor.status.value).toBe('submitted')
    expect(server.callsTo(POST)).toBe(2)
  })
})

describe('useFragmentEditor media', () => {
  it('takes a fresh audio url once and keeps the bounds and the stamp', async () => {
    const refreshed = makeFragmentView({
      audio_url: '/api/posts/7/audio?token=second',
      post_updated_at: '2026-10-09T13:00:00.000002Z',
      start_ms: 1,
      end_ms: 2,
    })
    const server = serverWith([makeFragmentView(), refreshed])
    const editor = await loaded(server)
    editor.nudgeEdge('start', 1000)

    expect(await editor.refreshMedia()).toBe(true)

    expect(editor.audioUrl.value).toBe('/api/posts/7/audio?token=second')
    expect(editor.bounds.value).toEqual({ startMs: 1000, endMs: 45_000 })
    await editor.submit()
    expect(lastBody(server)).toMatchObject({ expected_post_updated_at: MICRO_STAMP })
  })

  it('answers with a sentence instead of asking a second time', async () => {
    const server = serverWith([makeFragmentView()])
    const editor = await loaded(server)

    await editor.refreshMedia()
    expect(await editor.refreshMedia()).toBe(false)

    expect(server.callsTo(GET)).toBe(2)
    expect(editor.message.value).toBe(MEDIA_ERROR_MESSAGE)
  })

  it('asks again after the audio played', async () => {
    const server = serverWith([makeFragmentView()])
    const editor = await loaded(server)

    await editor.refreshMedia()
    editor.mediaPlayable()
    await editor.refreshMedia()

    expect(server.callsTo(GET)).toBe(3)
  })

  it('shows the server sentence when the fresh view cannot be fetched', async () => {
    let served = 0
    const server = fakeServer({
      [GET]: () =>
        served++ === 0
          ? jsonResponse(makeFragmentView())
          : errorResponse(409, 'audio_missing', 'Аудіо ще не завантажено.'),
    })
    const editor = await loaded(server)

    expect(await editor.refreshMedia()).toBe(false)

    expect(editor.message.value).toBe('Аудіо ще не завантажено.')
    expect(editor.audioUrl.value).toBe('/api/posts/7/audio?token=first')
  })
})
