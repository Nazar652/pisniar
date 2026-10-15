import { describe, expect, it, vi } from 'vitest'
import { NOTHING_TO_TAP_MESSAGE } from '../src/lyrics/timing'
import {
  EMPTY_PROPOSAL_MESSAGE,
  KEEP_OR_LOAD_QUESTION,
  NO_MORE_UNTIMED_MESSAGE,
  isMediaPath,
  MEDIA_ERROR_MESSAGE,
  RENDER_ONLY_MESSAGE,
  RENDER_STARTED_MESSAGE,
  SAVED_MESSAGE,
  useLyricsEditor,
} from '../src/lyrics/useLyricsEditor'
import {
  errorResponse,
  fakeServer,
  jsonResponse,
  makeFragmentView,
  makeLyricsView,
  makeProposal,
  MICRO_STAMP,
  type FakeServer,
  type Handler,
  type LyricsView,
} from './support'

const LYRICS = 'GET /api/posts/7/lyrics'
const SAVE = 'PUT /api/posts/7/lyrics'
const FRAGMENT = 'GET /api/posts/7/fragment'
const RENDER = 'POST /api/posts/7/fragment'
const SYNCED = 'GET /api/posts/7/lyrics/synced'
const PLAIN = 'GET /api/posts/7/lyrics/plain'
const STARTED = { post_id: 7, status_label: 'Рендериться', start_ms: 0, end_ms: 45_000 }

const STORED: LyricsView = makeLyricsView({
  lines: [
    { text: 'Перший', start_ms: 1000, source: 'lrclib' },
    { text: 'Другий', start_ms: null, source: 'manual' },
  ],
})

function lastBody(server: FakeServer, method: string): unknown {
  return server.bodies[server.requests.map((request) => request.method).lastIndexOf(method)]
}

function serverWith(extra: Record<string, Handler> = {}, view: LyricsView = STORED): FakeServer {
  return fakeServer({
    [LYRICS]: () => jsonResponse(view),
    [FRAGMENT]: () => jsonResponse(makeFragmentView()),
    [SAVE]: () => jsonResponse({ ...view, lyrics_version: 'v2' }),
    [RENDER]: () => jsonResponse(STARTED),
    ...extra,
  })
}

async function opened(server: FakeServer, answer = true, onSaved = vi.fn()) {
  const confirm = vi.fn(async () => answer)
  const editor = useLyricsEditor(server.client, 7, { confirm, onSaved })
  await editor.load()
  return { editor, confirm, onSaved }
}

describe('useLyricsEditor load', () => {
  it('starts loading and becomes ready with the stored lines, the audio and the limits', async () => {
    const server = serverWith()
    const editor = useLyricsEditor(server.client, 7, { confirm: async () => true })

    expect(editor.status.value).toBe('loading')
    await editor.load()

    expect(editor.status.value).toBe('ready')
    expect(editor.lines.value).toEqual([
      { text: 'Перший', startMs: 1000 },
      { text: 'Другий', startMs: null },
    ])
    expect(editor.buffer.value.cursor).toBe(1)
    expect(editor.audioUrl.value).toBe('/api/posts/7/audio?token=first')
    expect(editor.limits.value).toEqual({
      maxLines: 300,
      maxLineLength: 500,
      maxTotalCharacters: 20_000,
      durationMs: 200_000,
    })
    expect(editor.dirty.value).toBe(false)
  })

  it.each([
    ['lyrics', LYRICS, 'not_choosable', 409, 'Пост зараз не можна змінити.'],
    ['fragment', FRAGMENT, 'audio_missing', 409, 'Аудіо ще не завантажено.'],
  ])(
    'fails with the server sentence when the %s read is refused',
    async (_name, route, code, status, text) => {
      const server = serverWith({ [route]: () => errorResponse(status, code, text) })
      const editor = useLyricsEditor(server.client, 7, { confirm: async () => true })

      expect(await editor.load()).toBe(false)

      expect(editor.status.value).toBe('failed')
      expect(editor.message.value).toBe(text)
      expect(editor.view.value).toBeNull()
      expect(editor.needsReload.value).toBe(true)
    },
  )

  it('fails with the network sentence when the server cannot be reached', async () => {
    const server = serverWith({
      [LYRICS]: () => {
        throw new TypeError('offline')
      },
    })
    const editor = useLyricsEditor(server.client, 7, { confirm: async () => true })

    await editor.load()

    expect(editor.status.value).toBe('failed')
    expect(editor.message.value).toContain('Немає зв’язку')
  })
})

describe('useLyricsEditor timing', () => {
  it('taps only while the audio plays, and says so otherwise', async () => {
    const { editor } = await opened(serverWith())

    expect(editor.tapAt(5000)).toBe(false)
    expect(editor.message.value).toContain('відтворення')
    editor.setPlaying(true)
    expect(editor.status.value).toBe('timing')
    expect(editor.tapAt(5000)).toBe(true)
    expect(editor.lines.value[1]?.startMs).toBe(5000)
    editor.setPlaying(false)
    expect(editor.status.value).toBe('ready')
  })

  it('refuses a tap before the previous line with the sentence and changes nothing', async () => {
    const { editor } = await opened(serverWith())
    editor.setPlaying(true)

    expect(editor.tapAt(500)).toBe(false)

    expect(editor.message.value).toContain('раніше за попередній рядок')
    expect(editor.failed.value).toBe(true)
    expect(editor.dirty.value).toBe(false)
  })

  it('takes a tap back and answers where the audio should rewind to', async () => {
    const { editor } = await opened(serverWith())
    editor.setPlaying(true)
    editor.tapAt(8000)

    expect(editor.canUndo.value).toBe(true)
    expect(editor.undoLast()).toBe(8000)

    expect(editor.lines.value[1]?.startMs).toBeNull()
    expect(editor.buffer.value.cursor).toBe(1)
    expect(editor.dirty.value).toBe(false)
    expect(editor.undoLast()).toBeNull()
  })

  it('puts a music mark at the playhead as a stacked change and takes it back', async () => {
    const { editor } = await opened(serverWith())

    expect(editor.addMusicMark(500)).toBe(true)
    expect(editor.lines.value[0]).toEqual({ text: '♪', startMs: 500 })
    expect(editor.dirty.value).toBe(true)
    editor.undoLast()
    expect(editor.dirty.value).toBe(false)
    expect(editor.addMusicMark(1000)).toBe(false)
    expect(editor.message.value).toContain('уже є рядок')
  })

  it('forgets the undo stack when the text or the structure is edited', async () => {
    const { editor } = await opened(serverWith())
    editor.setPlaying(true)
    editor.tapAt(8000)

    editor.edit(0, 'Змінений')
    expect(editor.canUndo.value).toBe(false)
    editor.tapAt(9000)
    editor.addLine(0)
    expect(editor.canUndo.value).toBe(false)
  })

  it('compensates a tap by the playback speed it was made at', async () => {
    const { editor } = await opened(serverWith())
    editor.setPlaying(true)
    editor.setSpeed(0.5)

    editor.tapAt(8000)

    expect(editor.speed.value).toBe(0.5)
    expect(editor.lines.value[1]?.startMs).toBe(Math.round(8000 - 0 * 0.5))
  })

  it('marks the editor dirty after an edit and clean again when the text is back as stored', async () => {
    const { editor } = await opened(serverWith())

    editor.edit(1, 'Інший')
    expect(editor.dirty.value).toBe(true)
    editor.edit(1, 'Другий')
    expect(editor.dirty.value).toBe(false)
  })

  it('shows the first rule a line breaks and does not allow saving it', async () => {
    const { editor } = await opened(serverWith())

    editor.addLine(2)

    expect(editor.problem.value?.number).toBe(3)
    expect(editor.canSave.value).toBe(false)
    expect(editor.canRender.value).toBe(false)
  })
})

describe('useLyricsEditor save', () => {
  it('sends the lines and the version exactly as the server gave it and adopts the answer', async () => {
    const answer = makeLyricsView({
      lines: [
        { text: 'Перший', start_ms: 1000, source: 'manual' },
        { text: 'Другий', start_ms: 4000, source: 'manual' },
      ],
      lyrics_version: 'v2',
    })
    const server = serverWith({ [SAVE]: () => jsonResponse(answer) })
    const { editor, onSaved } = await opened(server)
    editor.setPlaying(true)
    editor.tapAt(4000)

    expect(await editor.save()).toBe(true)

    expect(lastBody(server, 'PUT')).toEqual({
      expected_lyrics_version: 'v1',
      lines: [
        { text: 'Перший', start_ms: 1000 },
        { text: 'Другий', start_ms: 4000 },
      ],
    })
    expect(editor.status.value).toBe('saved')
    expect(editor.message.value).toBe(SAVED_MESSAGE)
    expect(editor.dirty.value).toBe(false)
    expect(editor.view.value?.lyrics_version).toBe('v2')
    expect(onSaved).toHaveBeenCalledWith(false)
    expect(server.callsTo(RENDER)).toBe(0)
  })

  it('does nothing when nothing changed, and asks for no second request while saving', async () => {
    const server = serverWith()
    const { editor } = await opened(server)

    expect(await editor.save()).toBe(false)
    editor.edit(1, 'Інший')
    const first = editor.save()
    const second = await editor.save()
    await first

    expect(second).toBe(false)
    expect(server.callsTo(SAVE)).toBe(1)
  })

  it('saves and then queues the render with the bounds and the stamp of the fragment view', async () => {
    const server = serverWith({
      [FRAGMENT]: () =>
        jsonResponse(
          makeFragmentView({ start_ms: 10_000, end_ms: 50_000, post_updated_at: MICRO_STAMP }),
        ),
    })
    const { editor, onSaved } = await opened(server)
    editor.edit(1, 'Інший')

    expect(await editor.save(true)).toBe(true)

    expect(
      server.requests
        .map((request) => `${request.method} ${new URL(request.url).pathname}`)
        .slice(-2),
    ).toEqual([SAVE, RENDER])
    expect(lastBody(server, 'POST')).toEqual({
      start_ms: 10_000,
      end_ms: 50_000,
      expected_post_updated_at: '2026-10-09T12:34:56.123456Z',
    })
    expect(editor.message.value).toBe(RENDER_STARTED_MESSAGE)
    expect(editor.failed.value).toBe(false)
    expect(onSaved).toHaveBeenCalledWith(true)
  })

  it('queues only the render when the text is already stored', async () => {
    const server = serverWith()
    const { editor } = await opened(server)

    expect(await editor.save(true)).toBe(true)

    expect(server.callsTo(SAVE)).toBe(0)
    expect(server.callsTo(RENDER)).toBe(1)
    expect(editor.message.value).toBe(RENDER_ONLY_MESSAGE)
  })

  it('keeps the saved text and shows the sentence when the render is refused', async () => {
    const server = serverWith({
      [RENDER]: () =>
        errorResponse(503, 'dispatch_failed', 'Не вдалося поставити рендер. Надішліть /retry 7.'),
    })
    const { editor, onSaved } = await opened(server)
    editor.edit(1, 'Інший')

    await editor.save(true)

    expect(editor.dirty.value).toBe(false)
    expect(editor.message.value).toBe(
      `${SAVED_MESSAGE} Не вдалося поставити рендер. Надішліть /retry 7.`,
    )
    expect(editor.failed.value).toBe(true)
    expect(onSaved).toHaveBeenCalledWith(false)
  })

  it('takes a fresh stamp after a stale render so a second try can go through', async () => {
    let attempts = 0
    let views = 0
    const server = serverWith({
      [FRAGMENT]: () =>
        jsonResponse(
          makeFragmentView({
            post_updated_at: views++ === 0 ? MICRO_STAMP : '2026-10-09T13:00:00.000002Z',
          }),
        ),
      [RENDER]: () =>
        attempts++ === 0
          ? errorResponse(409, 'stale_data', 'Дані змінились відтоді, як ви їх відкрили.')
          : jsonResponse(STARTED),
    })
    const { editor } = await opened(server)

    await editor.save(true)
    await editor.save(true)

    expect(lastBody(server, 'POST')).toMatchObject({
      expected_post_updated_at: '2026-10-09T13:00:00.000002Z',
    })
    expect(editor.message.value).toBe(RENDER_ONLY_MESSAGE)
  })

  it('reloads on stale_data, adopts the stored text and shows the sentence', async () => {
    const fresh = makeLyricsView({
      lines: [{ text: 'Чужий', start_ms: 2000, source: 'manual' }],
      lyrics_version: 'v9',
    })
    let reads = 0
    const server = serverWith({
      [LYRICS]: () => jsonResponse(reads++ === 0 ? STORED : fresh),
      [SAVE]: () => errorResponse(409, 'stale_data', 'Дані змінились відтоді, як ви їх відкрили.'),
    })
    const { editor, confirm } = await opened(server, false)
    editor.edit(1, 'Інший')

    await editor.save()

    expect(confirm).toHaveBeenCalledWith(KEEP_OR_LOAD_QUESTION)
    expect(server.callsTo(LYRICS)).toBe(2)
    expect(server.callsTo(FRAGMENT)).toBe(2)
    expect(editor.status.value).toBe('ready')
    expect(editor.lines.value).toEqual([{ text: 'Чужий', startMs: 2000 }])
    expect(editor.view.value?.lyrics_version).toBe('v9')
    expect(editor.message.value).toBe('Дані змінились відтоді, як ви їх відкрили.')
    expect(editor.dirty.value).toBe(false)
  })

  it('keeps the buffer on stale_data when asked to, and refreshes only the version and the posts', async () => {
    const fresh = makeLyricsView({
      lines: [{ text: 'Чужий', start_ms: 2000, source: 'manual' }],
      lyrics_version: 'v9',
      other_post_ids: [4],
    })
    let reads = 0
    let saves = 0
    const server = serverWith({
      [LYRICS]: () => jsonResponse(reads++ === 0 ? STORED : fresh),
      [SAVE]: () =>
        saves++ === 0
          ? errorResponse(409, 'stale_data', 'Дані змінились відтоді, як ви їх відкрили.')
          : jsonResponse({ ...fresh, lyrics_version: 'v10' }),
    })
    const { editor, confirm, onSaved } = await opened(server, true)
    editor.edit(1, 'Інший')

    await editor.save()

    expect(confirm).toHaveBeenCalledWith(KEEP_OR_LOAD_QUESTION)
    expect(server.callsTo(LYRICS)).toBe(2)
    expect(server.callsTo(FRAGMENT)).toBe(1)
    expect(editor.lines.value[1]).toEqual({ text: 'Інший', startMs: null })
    expect(editor.dirty.value).toBe(true)
    expect(editor.view.value?.lyrics_version).toBe('v9')
    expect(editor.view.value?.other_post_ids).toEqual([4])
    expect(editor.message.value).toBe('Дані змінились відтоді, як ви їх відкрили.')
    expect(editor.failed.value).toBe(true)
    expect(editor.status.value).toBe('ready')
    expect(onSaved).not.toHaveBeenCalled()

    await editor.save()

    expect(lastBody(server, 'PUT')).toMatchObject({ expected_lyrics_version: 'v9' })
    expect(editor.status.value).toBe('saved')
  })

  it('keeps the editor usable when the refresh fails, and saves with the fresh version on a retry', async () => {
    const fresh = makeLyricsView({
      lines: [{ text: 'Чужий', start_ms: 2000, source: 'manual' }],
      lyrics_version: 'v9',
    })
    let reads = 0
    let saves = 0
    const server = serverWith({
      [LYRICS]: () =>
        reads++ === 0
          ? jsonResponse(STORED)
          : reads === 2
            ? errorResponse(503, 'database_unavailable', 'Сервер недоступний.')
            : jsonResponse(fresh),
      [SAVE]: () =>
        saves++ < 2
          ? errorResponse(409, 'stale_data', 'Дані змінились.')
          : jsonResponse({ ...fresh, lyrics_version: 'v10' }),
    })
    const { editor } = await opened(server, true)
    editor.edit(1, 'Інший')

    await editor.save()

    expect(editor.message.value).toBe('Сервер недоступний.')
    expect(editor.lines.value[1]?.text).toBe('Інший')
    expect(editor.needsReload.value).toBe(false)
    expect(editor.editable.value).toBe(true)
    expect(editor.canSave.value).toBe(true)

    await editor.save()

    expect(editor.view.value?.lyrics_version).toBe('v9')
    expect(editor.lines.value[1]?.text).toBe('Інший')
    expect(editor.dirty.value).toBe(true)

    await editor.save()

    expect(lastBody(server, 'PUT')).toMatchObject({ expected_lyrics_version: 'v9' })
    expect(editor.status.value).toBe('saved')
    expect(editor.dirty.value).toBe(false)
  })

  it.each([
    ['post_busy', 409, 'Зайнято, спробуйте за мить.', false],
    ['media_in_progress', 409, 'Для цього треку зараз готується відео.', false],
    ['invalid_lyrics', 422, 'Час рядка 2 (5 мс) не пізніший за попередній.', false],
    ['database_unavailable', 503, 'Сервер зараз недоступний.', false],
    ['unknown', 500, 'Щось пішло не так.', false],
    ['not_choosable', 409, 'Пост зараз не можна змінити.', true],
    ['audio_missing', 409, 'Аудіо ще не завантажено.', true],
    ['post_not_found', 404, 'Пост не знайдено.', true],
  ])(
    'shows the sentence of %s, keeps the buffer and ends %s',
    async (code, httpStatus, text, dead) => {
      const server = serverWith({ [SAVE]: () => errorResponse(httpStatus, code, text) })
      const { editor } = await opened(server)
      editor.edit(1, 'Інший')

      await editor.save()

      expect(editor.status.value).toBe('failed')
      expect(editor.message.value).toBe(text)
      expect(editor.lines.value[1]?.text).toBe('Інший')
      expect(editor.needsReload.value).toBe(dead)
      expect(editor.canSave.value).toBe(!dead)
    },
  )

  it('lets the admin try again after a refusal that leaves the editor open', async () => {
    let attempts = 0
    const server = serverWith({
      [SAVE]: () =>
        attempts++ === 0
          ? errorResponse(503, 'database_unavailable', 'Сервер зараз недоступний.')
          : jsonResponse({ ...STORED, lyrics_version: 'v2' }),
    })
    const { editor } = await opened(server)
    editor.edit(1, 'Інший')

    await editor.save()
    expect(editor.status.value).toBe('failed')
    expect(await editor.save()).toBe(true)

    expect(editor.status.value).toBe('saved')
    expect(server.callsTo(SAVE)).toBe(2)
  })

  it('returns to ready on the next edit after a save', async () => {
    const { editor } = await opened(serverWith())
    editor.edit(1, 'Інший')
    await editor.save()

    editor.edit(1, 'Ще')

    expect(editor.status.value).toBe('ready')
  })
})

const LONGER: LyricsView = makeLyricsView({
  lines: [
    { text: 'Перший', start_ms: 1000, source: 'lrclib' },
    { text: 'Другий', start_ms: null, source: 'manual' },
    { text: 'Третій', start_ms: null, source: 'manual' },
    { text: 'Четвертий', start_ms: null, source: 'manual' },
  ],
})

describe('useLyricsEditor tapping while the audio keeps playing', () => {
  it('taps again after a save', async () => {
    const { editor } = await opened(serverWith({}, LONGER))
    editor.setPlaying(true)
    editor.tapAt(5000)

    await editor.save()

    expect(editor.status.value).toBe('saved')
    expect(editor.tapAt(6000)).toBe(true)
    expect(editor.lines.value[2]?.startMs).toBe(6000)
  })

  it('taps again after a failed save', async () => {
    const server = serverWith(
      { [SAVE]: () => errorResponse(503, 'database_unavailable', 'Сервер зараз недоступний.') },
      LONGER,
    )
    const { editor } = await opened(server)
    editor.setPlaying(true)
    editor.tapAt(5000)

    await editor.save()

    expect(editor.status.value).toBe('failed')
    expect(editor.tapAt(6000)).toBe(true)
    expect(editor.status.value).toBe('timing')
  })

  it.each([true, false])('taps again after a stale_data answer (keep: %s)', async (keep) => {
    const server = serverWith(
      {
        [SAVE]: () =>
          errorResponse(409, 'stale_data', 'Дані змінились відтоді, як ви їх відкрили.'),
      },
      LONGER,
    )
    const { editor } = await opened(server, keep)
    editor.setPlaying(true)
    editor.tapAt(5000)

    await editor.save()

    expect(editor.status.value).toBe('timing')
    expect(editor.tapAt(6000)).toBe(true)
  })

  it('refuses a tap once the audio is paused, whatever the status was', async () => {
    const { editor } = await opened(serverWith())
    editor.setPlaying(true)
    editor.setPlaying(false)

    expect(editor.tapAt(5000)).toBe(false)
    expect(editor.message.value).toContain('відтворення')
  })
})

describe('useLyricsEditor next untimed line', () => {
  it('moves the cursor to the next line without a time and says so when there is none', async () => {
    const { editor } = await opened(serverWith({}, LONGER))

    expect(editor.buffer.value.cursor).toBe(1)
    expect(editor.toNextUntimed()).toBe(true)
    expect(editor.buffer.value.cursor).toBe(2)
    editor.toNextUntimed()
    expect(editor.toNextUntimed()).toBe(false)
    expect(editor.message.value).toBe(NO_MORE_UNTIMED_MESSAGE)
    expect(editor.buffer.value.cursor).toBe(3)
    expect(editor.dirty.value).toBe(false)
  })
})

describe('useLyricsEditor media address', () => {
  it.each([
    ['/api/posts/7/audio?token=a', true],
    ['https://evil.test/api/x', false],
    ['//evil.test/api/x', false],
    ['/other/path', false],
    ['api/posts/7/audio', false],
    ['/api/\\evil', false],
    ['javascript:alert(1)', false],
  ])('judges %s as same-origin api path: %s', (url, expected) => {
    expect(isMediaPath(url)).toBe(expected)
  })

  it('treats a foreign address at load as a media error and gives the audio no source', async () => {
    const server = serverWith({
      [FRAGMENT]: () => jsonResponse(makeFragmentView({ audio_url: 'https://evil.test/a.mp3' })),
    })
    const { editor } = await opened(server)

    expect(editor.audioUrl.value).toBe('')
    expect(editor.message.value).toBe(MEDIA_ERROR_MESSAGE)
    expect(editor.failed.value).toBe(true)
    expect(editor.status.value).toBe('ready')
  })

  it('refuses a foreign address on a refresh and keeps the current one', async () => {
    let views = 0
    const server = serverWith({
      [FRAGMENT]: () =>
        jsonResponse(
          makeFragmentView({
            audio_url: views++ === 0 ? '/api/posts/7/audio?token=a' : '//evil.test/x',
          }),
        ),
    })
    const { editor } = await opened(server)

    expect(await editor.refreshMedia()).toBe(false)

    expect(editor.audioUrl.value).toBe('/api/posts/7/audio?token=a')
    expect(editor.message.value).toBe(MEDIA_ERROR_MESSAGE)
  })

  it('stops tapping when the audio fails, since it no longer plays', async () => {
    const { editor } = await opened(serverWith())
    editor.setPlaying(true)
    expect(editor.status.value).toBe('timing')

    await editor.refreshMedia()

    expect(editor.playing.value).toBe(false)
    expect(editor.status.value).toBe('ready')
    expect(editor.tapAt(5000)).toBe(false)
  })
})

describe('useLyricsEditor media', () => {
  it('takes a fresh audio url once and answers with a sentence the second time', async () => {
    let views = 0
    const server = serverWith({
      [FRAGMENT]: () =>
        jsonResponse(makeFragmentView({ audio_url: `/api/posts/7/audio?token=v${views++}` })),
    })
    const { editor } = await opened(server)

    expect(await editor.refreshMedia()).toBe(true)
    expect(editor.audioUrl.value).toBe('/api/posts/7/audio?token=v1')
    expect(await editor.refreshMedia()).toBe(false)
    expect(editor.message.value).toBe(MEDIA_ERROR_MESSAGE)
    expect(server.callsTo(FRAGMENT)).toBe(2)
    editor.mediaPlayable()
    expect(await editor.refreshMedia()).toBe(true)
  })
})

describe('«Знайти синхронізацію»', () => {
  const found = makeProposal()

  it('asks before replacing a non-empty buffer and replaces it with the proposal, dirty and unsaved', async () => {
    const server = serverWith({ [SYNCED]: () => jsonResponse(found) })
    const { editor, confirm } = await opened(server)

    expect(await editor.findSynced()).toBe(true)

    expect(confirm).toHaveBeenCalledWith(
      'Замінити поточний текст (2 рядки) знайденим (2 рядки із часом, LRCLIB)?',
    )
    expect(editor.lines.value).toEqual([
      { text: 'Перший', startMs: 1000 },
      { text: 'Другий', startMs: 5000 },
    ])
    expect(editor.buffer.value.cursor).toBe(2)
    expect(editor.tapText.value).toBe('Усі рядки відстукано')
    expect(editor.dirty.value).toBe(true)
    expect(editor.canUndo.value).toBe(false)
    expect(server.callsTo(SAVE)).toBe(0)
  })

  it('leaves the buffer as it was when the admin says no', async () => {
    const server = serverWith({ [SYNCED]: () => jsonResponse(found) })
    const { editor } = await opened(server, false)

    expect(await editor.findSynced()).toBe(false)

    expect(editor.lines.value).toEqual([
      { text: 'Перший', startMs: 1000 },
      { text: 'Другий', startMs: null },
    ])
    expect(editor.dirty.value).toBe(false)
    expect(editor.looking.value).toBe(false)
  })

  it('does not ask when the buffer is empty', async () => {
    const server = serverWith({ [SYNCED]: () => jsonResponse(found) }, makeLyricsView())
    const { editor, confirm } = await opened(server)

    expect(await editor.findSynced()).toBe(true)

    expect(confirm).not.toHaveBeenCalled()
    expect(editor.lines.value).toHaveLength(2)
  })

  it('mentions what the server dropped or cut', async () => {
    const server = serverWith(
      {
        [SYNCED]: () =>
          jsonResponse(makeProposal({ dropped_line_count: 3, truncated_line_count: 1 })),
      },
      makeLyricsView(),
    )
    const { editor } = await opened(server)

    await editor.findSynced()

    expect(editor.message.value).toContain('пропущено 3 рядки')
    expect(editor.message.value).toContain('скорочено 1 рядок')
  })

  it.each([
    ['synced_lyrics_not_found', 409, 'Тексту з таймкодами для цього треку не знайдено.', false],
    ['lyrics_source_unavailable', 503, 'Джерело тексту не відповіло. Спробуйте за хвилину.', false],
    ['lyrics_over_limits', 409, 'Знайдений текст завеликий, щоб його зберегти.', false],
    ['not_choosable', 409, 'Пост зараз не можна змінити.', true],
  ])('shows the server sentence of %s and keeps the buffer', async (code, status, text, dead) => {
    const server = serverWith({ [SYNCED]: () => errorResponse(status, code, text) })
    const { editor, confirm } = await opened(server)

    expect(await editor.findSynced()).toBe(false)

    expect(editor.message.value).toBe(text)
    expect(editor.failed.value).toBe(true)
    expect(editor.dirty.value).toBe(false)
    expect(editor.needsReload.value).toBe(dead)
    expect(confirm).not.toHaveBeenCalled()
  })

  it('can be repeated after the source was unavailable', async () => {
    let attempts = 0
    const server = serverWith({
      [SYNCED]: () =>
        attempts++ === 0
          ? errorResponse(503, 'lyrics_source_unavailable', 'Джерело тексту не відповіло.')
          : jsonResponse(found),
    })
    const { editor } = await opened(server)

    expect(await editor.findSynced()).toBe(false)
    expect(await editor.findSynced()).toBe(true)

    expect(server.callsTo(SYNCED)).toBe(2)
    expect(editor.dirty.value).toBe(true)
  })

  it('refuses an empty proposal with a sentence of its own', async () => {
    const server = serverWith({ [SYNCED]: () => jsonResponse(makeProposal({ lines: [] })) })
    const { editor } = await opened(server)

    expect(await editor.findSynced()).toBe(false)

    expect(editor.message.value).toBe(EMPTY_PROPOSAL_MESSAGE)
  })

  it('blocks the other actions while it runs', async () => {
    let release: (response: Response) => void = () => undefined
    const answer = new Promise<Response>((resolve) => (release = resolve))
    const server = serverWith({ [SYNCED]: () => answer })
    const { editor } = await opened(server)

    const pending = editor.findSynced()
    expect(editor.looking.value).toBe(true)
    expect(editor.editable.value).toBe(false)
    expect(editor.addMusicMark(500)).toBe(false)
    release(errorResponse(409, 'synced_lyrics_not_found', 'Не знайдено.'))
    await pending

    expect(editor.looking.value).toBe(false)
  })
})

describe('«Знайти просто текст»', () => {
  const plain = makeProposal({
    kind: 'plain',
    source: 'genius',
    lines: [
      { text: 'Рядок один', start_ms: null },
      { text: 'Рядок два', start_ms: null },
      { text: 'Рядок три', start_ms: null },
    ],
  })

  it('asks Genius by default, confirms for a non-empty buffer and leaves every line untimed', async () => {
    const server = serverWith({ [PLAIN]: () => jsonResponse(plain) })
    const { editor, confirm } = await opened(server)

    expect(editor.plainSource.value).toBe('genius')
    expect(await editor.findPlain()).toBe(true)

    const request = server.requests.find((entry) => new URL(entry.url).pathname.endsWith('/plain'))
    expect(new URL(request?.url ?? 'https://x.test').searchParams.get('source')).toBe('genius')
    expect(confirm).toHaveBeenCalledWith(
      'Замінити поточний текст (2 рядки) текстом із Genius (3 рядки без часу)?',
    )
    expect(editor.lines.value.every((line) => line.startMs === null)).toBe(true)
    expect(editor.buffer.value.cursor).toBe(0)
    expect(editor.dirty.value).toBe(true)
  })

  it.each(['ytmusic', 'lrclib'] as const)('asks the chosen source %s', async (source) => {
    const server = serverWith({ [PLAIN]: () => jsonResponse({ ...plain, source }) })
    const { editor } = await opened(server, true)
    editor.setPlainSource(source)

    await editor.findPlain()

    const request = server.requests.find((entry) => new URL(entry.url).pathname.endsWith('/plain'))
    expect(new URL(request?.url ?? 'https://x.test').searchParams.get('source')).toBe(source)
  })

  it('leaves the buffer after a no, and after the sentence that suggests another source', async () => {
    const refusedServer = serverWith({
      [PLAIN]: () =>
        errorResponse(
          409,
          'plain_lyrics_not_found',
          'У Genius тексту цього треку не знайдено. Спробуйте інше джерело.',
        ),
    })
    const refused = await opened(refusedServer)
    expect(await refused.editor.findPlain()).toBe(false)
    expect(refused.editor.message.value).toContain('Спробуйте інше джерело.')
    expect(refused.editor.dirty.value).toBe(false)

    const declined = await opened(serverWith({ [PLAIN]: () => jsonResponse(plain) }), false)
    expect(await declined.editor.findPlain()).toBe(false)
    expect(declined.editor.lines.value).toHaveLength(2)
  })

  it('does not ask for an empty buffer', async () => {
    const server = serverWith({ [PLAIN]: () => jsonResponse(plain) }, makeLyricsView())
    const { editor, confirm } = await opened(server)

    await editor.findPlain()

    expect(confirm).not.toHaveBeenCalled()
    expect(editor.lines.value).toHaveLength(3)
  })
})

describe('«♪» and pasted text', () => {
  it('replaces a timed list only after a yes and a list without times at once', async () => {
    const timed = await opened(serverWith(), false)
    expect(await timed.editor.paste('Новий\nТекст')).toBe(false)
    expect(timed.confirm).toHaveBeenCalledOnce()
    expect(timed.editor.lines.value).toHaveLength(2)
    expect(timed.editor.lines.value[0]?.text).toBe('Перший')

    const accepted = await opened(serverWith(), true)
    expect(await accepted.editor.paste('Новий\nТекст\nТретій')).toBe(true)
    expect(accepted.editor.lines.value).toEqual([
      { text: 'Новий', startMs: null },
      { text: 'Текст', startMs: null },
      { text: 'Третій', startMs: null },
    ])

    const untimed = await opened(
      serverWith({}, makeLyricsView({ lines: [{ text: 'a', start_ms: null, source: 'manual' }] })),
    )
    await untimed.editor.paste('Інший')
    expect(untimed.confirm).not.toHaveBeenCalled()
    expect(untimed.editor.lines.value).toEqual([{ text: 'Інший', startMs: null }])
  })

  it('pastes nothing for blank text', async () => {
    const { editor, confirm } = await opened(serverWith())

    expect(await editor.paste('  \n ')).toBe(false)
    expect(confirm).not.toHaveBeenCalled()
  })
})

describe('tapping from a line again (the «Відстукати звідси» flow)', () => {
  const ALL_TIMED: LyricsView = makeLyricsView({
    lines: Array.from({ length: 8 }, (_, index) => ({
      text: `Рядок ${index + 1}`,
      start_ms: (index + 1) * 10_000,
      source: 'manual' as const,
    })),
  })

  function times(editor: { lines: { value: readonly { startMs: number | null }[] } }) {
    return editor.lines.value.map((line) => line.startMs)
  }

  function rising(values: (number | null)[]): boolean {
    const timed = values.filter((value): value is number => value !== null)
    return timed.every((value, index) => index === 0 || value > (timed[index - 1] ?? -1))
  }

  it('keeps the timed lines rising after every tap and leaves the later times above the tap', async () => {
    const { editor } = await opened(serverWith({}, ALL_TIMED))

    editor.retime(2)
    editor.setPlaying(true)
    expect(editor.tapAt(65_000)).toBe(true)

    // the lines the tap passed lose their time; the two above it keep theirs, in order
    expect(times(editor)).toEqual([10_000, 20_000, 65_000, null, null, null, 70_000, 80_000])
    expect(rising(times(editor))).toBe(true)

    for (const position of [66_000, 67_000, 68_000]) {
      expect(editor.tapAt(position)).toBe(true)
      expect(rising(times(editor))).toBe(true)
    }
    expect(times(editor)).toEqual([10_000, 20_000, 65_000, 66_000, 67_000, 68_000, 70_000, 80_000])
    expect(editor.buffer.value.cursor).toBe(6)
  })

  it('refuses a tap that would break the order and changes nothing', async () => {
    const { editor } = await opened(serverWith({}, ALL_TIMED))
    editor.retime(2)
    editor.setPlaying(true)
    const before = times(editor)

    expect(editor.tapAt(20_000)).toBe(false)

    expect(times(editor)).toEqual(before)
  })

  it('stays rising through retime, taps, undo and a second retime', async () => {
    const { editor } = await opened(serverWith({}, ALL_TIMED))
    editor.setPlaying(true)

    for (const [from, positions] of [
      [5, [55_000, 56_000]],
      [1, [25_000, 26_000, 27_000]],
      [0, [5_000]],
    ] as const) {
      editor.retime(from)
      for (const position of positions) {
        editor.tapAt(position)
        expect(rising(times(editor))).toBe(true)
      }
      editor.undoLast()
      expect(rising(times(editor))).toBe(true)
    }
    expect(editor.problem.value).toBeNull()
  })
})

describe('the tap cursor and the glass preview', () => {
  const TIMED: LyricsView = makeLyricsView({
    lines: [
      { text: 'Перший', start_ms: 1000, source: 'lrclib' },
      { text: '', start_ms: 5000, source: 'manual' },
      { text: 'Третій', start_ms: 9000, source: 'lrclib' },
    ],
  })

  it('stands on the first untimed line when a text is loaded', async () => {
    const { editor } = await opened(serverWith())

    expect(editor.buffer.value.cursor).toBe(1)
    expect(editor.tapText.value).toBe('Тап → Другий')
  })

  it('stands past the end when every line is timed, and a tap there changes nothing', async () => {
    const { editor } = await opened(serverWith({}, TIMED))
    editor.setPlaying(true)
    const before = editor.lines.value

    expect(editor.buffer.value.cursor).toBe(3)
    expect(editor.tapText.value).toBe('Усі рядки відстукано')
    expect(editor.tapAt(20_000)).toBe(false)

    expect(editor.message.value).toBe(NOTHING_TO_TAP_MESSAGE)
    expect(editor.lines.value).toBe(before)
  })

  it('names a pause «(програш)» on the tap button', async () => {
    const { editor } = await opened(serverWith({}, TIMED))

    editor.retime(1)

    expect(editor.tapText.value).toBe('Тап → (програш)')
  })

  it('lets a chosen row and the next-untimed step move the cursor as before', async () => {
    const { editor } = await opened(serverWith())

    editor.retime(0)
    expect(editor.buffer.value.cursor).toBe(0)
    expect(editor.toNextUntimed()).toBe(true)
    expect(editor.buffer.value.cursor).toBe(1)
  })

  it('goes to the first untimed line after pasted text replaces a timed one', async () => {
    const { editor } = await opened(serverWith({}, TIMED))

    expect(await editor.paste('Новий\nТекст')).toBe(true)

    expect(editor.buffer.value.cursor).toBe(0)
    expect(editor.tapText.value).toBe('Тап → Новий')
  })

  it('goes past the end after a found synced text', async () => {
    const synced = await opened(serverWith({ [SYNCED]: () => jsonResponse(makeProposal()) }))
    await synced.editor.findSynced()

    expect(synced.editor.buffer.value.cursor).toBe(2)
    expect(synced.editor.tapText.value).toBe('Усі рядки відстукано')
  })

  it('goes to the first line after a found plain text, which has no times', async () => {
    const plainProposal = makeProposal({
      kind: 'plain',
      source: 'genius',
      lines: [
        { text: 'Новий', start_ms: null },
        { text: 'Текст', start_ms: null },
      ],
    })
    const plain = await opened(serverWith({ [PLAIN]: () => jsonResponse(plainProposal) }, TIMED))
    plain.editor.retime(2)

    await plain.editor.findPlain()

    expect(plain.editor.buffer.value.cursor).toBe(0)
    expect(plain.editor.tapText.value).toBe('Тап → Новий')
  })

  it('shows the glass at the audio position, taken from the timed lines', async () => {
    const { editor } = await opened(serverWith({}, TIMED))

    editor.setPosition(2000)
    expect(editor.glass.value).toEqual({ previous: null, current: 0, next: 2, active: 0 })
    editor.setPosition(6000)
    expect(editor.glass.value).toEqual({ previous: 0, current: null, next: 2, active: 1 })
    editor.setPosition(500)
    expect(editor.glass.value.active).toBeNull()
  })

  it('marks the sounding row only while the audio plays, and follows it then', async () => {
    const { editor } = await opened(serverWith({}, TIMED))
    editor.setPosition(2000)

    expect(editor.soundingRow.value).toBeNull()
    expect(editor.followedRow.value).toBeNull()

    editor.setPlaying(true)
    expect(editor.soundingRow.value).toBe(0)
    expect(editor.followedRow.value).toBe(0)

    editor.setPosition(10_000)
    expect(editor.followedRow.value).toBe(2)

    editor.setPosition(100)
    expect(editor.soundingRow.value).toBeNull()
    expect(editor.followedRow.value).toBeNull()

    editor.setPosition(2000)
    editor.setPlaying(false)
    expect(editor.followedRow.value).toBeNull()
  })

  it('takes a slot to its line time without touching the playing state', async () => {
    const { editor } = await opened(serverWith({}, TIMED))
    editor.setPosition(6000)
    const { previous, current, next } = editor.glass.value

    expect(editor.slotStart(previous)).toBe(1000)
    expect(editor.slotStart(next)).toBe(9000)
    expect(editor.slotStart(current)).toBeNull()
    expect(editor.playing.value).toBe(false)
    expect(editor.status.value).toBe('ready')
  })
})
