import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  CONFIRM_APPLY_TEXT,
  CONFIRM_ARCHIVE_TEXT,
  CONFIRM_COVER_TEXT,
  COVER_PUBLISHING_MESSAGE,
  INVALID_YEAR_MESSAGE,
  NOTHING_TO_APPLY_MESSAGE,
  usePostEditor,
  type EditorDeps,
} from '../src/editor/usePostEditor'
import { NETWORK_FAILURE_MESSAGE } from '../src/api/client'
import type { Confirm, Haptic } from '../src/telegram/actions'
import {
  editorServer,
  errorResponse,
  jsonResponse,
  makeCaption,
  makeDetail,
  type FakeServer,
} from './support'

const SHA = 'b'.repeat(64)
const SAVED_KEY = 'PUT /api/posts/7/fields'
const APPLY_KEY = 'POST /api/posts/7/apply'
const ARCHIVE_KEY = 'POST /api/posts/7/archive'

function makeDeps(confirmed = true) {
  const confirm = vi.fn<Confirm>(() => Promise.resolve(confirmed))
  const haptic = vi.fn<Haptic>()
  return { confirm, haptic, debounceMs: 10 }
}

async function ready(
  server: FakeServer,
  deps: EditorDeps & ReturnType<typeof makeDeps> = makeDeps(),
) {
  const editor = usePostEditor(server.client, 7, deps)
  await editor.load()
  return { editor, deps }
}

const savedDetail = (overrides = {}) =>
  makeDetail({ title: 'Нова', caption: makeCaption({ sha256: SHA }), ...overrides })

const appliedReply = (result = 'edited', failed: string[] = []) =>
  jsonResponse({ caption_sha256: SHA, result, other_messages_failed: failed })

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('loading', () => {
  it('reads the post and the static facts and builds the form', async () => {
    const server = editorServer(makeDetail())

    const { editor } = await ready(server)

    expect(editor.status.value).toBe('ready')
    expect(editor.form.value?.title).toBe('Тема')
    expect(editor.form.value?.artists.map((artist) => artist.name)).toEqual([
      'Океан Ельзи',
      'Друга Ріка',
    ])
    expect(editor.meta.value?.caption_limit).toBe(1024)
    expect(editor.dirty.value).toBe(false)
  })

  it('shows the server sentence when the post cannot be read', async () => {
    const server = editorServer(makeDetail(), {
      'GET /api/posts/7': () => errorResponse(404, 'post_not_found', 'Пост не знайдено.'),
    })

    const { editor } = await ready(server)

    expect(editor.status.value).toBe('failed')
    expect(editor.loadMessage.value).toBe('Пост не знайдено.')
  })

  it('fails with the network sentence when the connection is down', async () => {
    const server = editorServer(makeDetail(), {
      'GET /api/posts/7': () => {
        throw new TypeError('offline')
      },
    })

    const { editor } = await ready(server)

    expect(editor.status.value).toBe('failed')
    expect(editor.loadMessage.value).toBe(NETWORK_FAILURE_MESSAGE)
  })

  it('asks for the static facts once however often the post is reloaded', async () => {
    const server = editorServer(makeDetail())
    const { editor } = await ready(server)

    await editor.load(true)

    expect(server.callsTo('GET /api/meta')).toBe(1)
    expect(server.callsTo('GET /api/posts/7')).toBe(2)
  })

  it('reports the number and ids of the other posts of the track', async () => {
    const server = editorServer(makeDetail({ other_post_ids: [9, 8] }))

    const { editor } = await ready(server)

    expect(editor.detail.value?.other_post_ids).toEqual([9, 8])
  })
})

describe('preview', () => {
  it('waits for the typing to stop and sends one request with the unsaved fields', async () => {
    const server = editorServer(makeDetail())
    const { editor } = await ready(server)
    await vi.advanceTimersByTimeAsync(50)
    const before = server.callsTo('POST /api/posts/7/preview')

    for (const title of ['Н', 'Но', 'Нов']) {
      if (editor.form.value) editor.form.value.title = title
      await vi.advanceTimersByTimeAsync(3)
    }
    await vi.advanceTimersByTimeAsync(50)

    expect(server.callsTo('POST /api/posts/7/preview') - before).toBe(1)
    const last = server.bodies[server.bodies.length - 1] as { title: string }
    expect(last.title).toBe('Нов')
    expect(editor.preview.value?.visible_length).toBe(4)
  })

  it('keeps the answer to the latest request when an older one arrives late', async () => {
    let release: (value: Response) => void = () => undefined
    let calls = 0
    const server = editorServer(makeDetail(), {
      'POST /api/posts/7/preview': () => {
        calls += 1
        if (calls === 1) return new Promise<Response>((resolve) => (release = resolve))
        return jsonResponse({
          caption: makeCaption({ visible_length: 99 }),
          stored_caption_sha256: null,
          differs_from_stored: true,
        })
      },
    })
    const { editor } = await ready(server)
    await vi.advanceTimersByTimeAsync(20)
    if (editor.form.value) editor.form.value.title = 'Друга'
    await vi.advanceTimersByTimeAsync(20)

    release(
      jsonResponse({
        caption: makeCaption({ visible_length: 1 }),
        stored_caption_sha256: null,
        differs_from_stored: true,
      }),
    )
    await vi.advanceTimersByTimeAsync(20)

    expect(editor.preview.value?.visible_length).toBe(99)
  })

  it('shows the validation errors next to the fields', async () => {
    const server = editorServer(makeDetail(), {
      'POST /api/posts/7/preview': () =>
        errorResponse(422, 'invalid_fields', 'Деякі поля заповнено неправильно.', {
          errors: [{ field: 'artists[0].hashtag', message: 'Хештег недійсний.' }],
        }),
    })
    const { editor } = await ready(server)

    await vi.advanceTimersByTimeAsync(20)

    expect(editor.errorOf('artists[0].hashtag')).toBe('Хештег недійсний.')
    expect(editor.previewMessage.value).toBe('Деякі поля заповнено неправильно.')
  })

  it('does not ask for a preview of a year that is not a number', async () => {
    const server = editorServer(makeDetail())
    const { editor } = await ready(server)
    await vi.advanceTimersByTimeAsync(20)
    const before = server.callsTo('POST /api/posts/7/preview')

    if (editor.form.value) editor.form.value.year = 'abc'
    await vi.advanceTimersByTimeAsync(50)

    expect(server.callsTo('POST /api/posts/7/preview')).toBe(before)
  })
})

describe('saving before publication', () => {
  function serverFor(extra = {}) {
    return editorServer(makeDetail(), {
      [SAVED_KEY]: () => jsonResponse(savedDetail()),
      [APPLY_KEY]: () => appliedReply(),
      ...extra,
    })
  }

  it('saves the fields and then applies the caption with the hash the save returned', async () => {
    const server = serverFor()
    const { editor, deps } = await ready(server)
    if (editor.form.value) editor.form.value.title = 'Нова'

    await editor.save()

    const order = server.requests
      .map((request) => `${request.method} ${new URL(request.url).pathname}`)
      .filter((key) => key === SAVED_KEY || key === APPLY_KEY)
    expect(order).toEqual([SAVED_KEY, APPLY_KEY])
    const sent = server.bodies[server.requests.findIndex((r) => r.method === 'PUT')] as {
      title: string
      expected_post_updated_at: string
    }
    expect(sent.title).toBe('Нова')
    expect(sent.expected_post_updated_at).toBe('2026-10-02T12:00:00Z')
    const apply = server.bodies[server.requests.findIndex((r) => r.url.endsWith('/apply'))]
    expect(apply).toEqual({ expected_caption_sha256: SHA })
    expect(editor.notice.value).toEqual({ kind: 'success', text: 'Збережено й застосовано.' })
    expect(deps.haptic).toHaveBeenCalledWith('success')
    expect(editor.busy.value).toBe(false)
  })

  it('says so when the message already carried the caption', async () => {
    const { editor } = await ready(serverFor({ [APPLY_KEY]: () => appliedReply('not_modified') }))

    await editor.save()

    expect(editor.notice.value?.text).toContain('вже мало цей підпис')
  })

  it('names the messages that could not be updated after the commit', async () => {
    const { editor } = await ready(
      serverFor({ [APPLY_KEY]: () => appliedReply('edited', ['draft']) }),
    )

    await editor.save()

    expect(editor.notice.value?.kind).toBe('info')
    expect(editor.notice.value?.text).toContain('чернетку в чаті')
  })

  it('does not apply when the save is refused for invalid fields, and marks them', async () => {
    const server = serverFor({
      [SAVED_KEY]: () =>
        errorResponse(422, 'invalid_fields', 'Деякі поля заповнено неправильно.', {
          errors: [{ field: 'platform_links.tidal', message: 'Не той хост.' }],
        }),
    })
    const { editor, deps } = await ready(server)

    await editor.save()

    expect(server.callsTo(APPLY_KEY)).toBe(0)
    expect(editor.errorOf('platform_links.tidal')).toBe('Не той хост.')
    expect(editor.notice.value?.kind).toBe('error')
    expect(deps.haptic).toHaveBeenCalledWith('error')
  })

  it('reloads and shows the sentence when the data changed since it was opened', async () => {
    const server = serverFor({
      [SAVED_KEY]: () => errorResponse(409, 'stale_data', 'Дані змінились. Перегляньте їх ще раз.'),
    })
    const { editor } = await ready(server)

    await editor.save()

    expect(server.callsTo('GET /api/posts/7')).toBe(2)
    expect(editor.notice.value).toEqual({
      kind: 'error',
      text: 'Дані змінились. Перегляньте їх ще раз.',
    })
    expect(server.callsTo(APPLY_KEY)).toBe(0)
  })

  it('reloads when the caption changed between the save and the apply', async () => {
    const server = serverFor({
      [APPLY_KEY]: () => errorResponse(409, 'caption_changed', 'Підпис змінився.'),
    })
    const { editor } = await ready(server)

    await editor.save()

    expect(server.callsTo('GET /api/posts/7')).toBe(2)
    expect(editor.notice.value?.text).toBe('Підпис змінився.')
  })

  it('offers archiving only after the channel message was reported missing', async () => {
    const server = serverFor({
      [APPLY_KEY]: () => errorResponse(409, 'channel_message_missing', 'Повідомлення видалено.'),
    })
    const { editor } = await ready(server)
    expect(editor.channelMissing.value).toBe(false)

    await editor.save()

    expect(editor.channelMissing.value).toBe(true)
    expect(editor.notice.value?.text).toBe('Повідомлення видалено.')
  })

  it.each([
    [429, 'telegram_rate_limited', 'Telegram просить зачекати 17 с.'],
    [502, 'telegram_failed', 'Telegram не прийняв зміну.'],
    [503, 'database_unavailable', 'База даних зараз недоступна.'],
    [422, 'caption_too_long', 'Підпис задовгий: 1100 символів із 1024.'],
    [409, 'media_in_progress', 'Посилання змінити не можна.'],
  ])('shows the server sentence for %i %s as it is', async (status, code, message) => {
    const server = serverFor({ [SAVED_KEY]: () => errorResponse(status, code, message) })
    const { editor } = await ready(server)

    await editor.save()

    expect(editor.notice.value).toEqual({ kind: 'error', text: message })
    expect(server.callsTo(APPLY_KEY)).toBe(0)
  })

  it.each([
    [429, 'telegram_rate_limited'],
    [502, 'telegram_failed'],
    [409, 'channel_message_exists'],
    [503, 'database_unavailable'],
  ])('does not offer archiving after %i %s', async (status, code) => {
    const server = serverFor({ [APPLY_KEY]: () => errorResponse(status, code, 'Речення.') })
    const { editor } = await ready(server)

    await editor.save()

    expect(editor.channelMissing.value).toBe(false)
  })

  it('reports a network failure with its own sentence', async () => {
    const server = serverFor({
      [SAVED_KEY]: () => {
        throw new TypeError('offline')
      },
    })
    const { editor } = await ready(server)

    await editor.save()

    expect(editor.notice.value?.text).toBe(NETWORK_FAILURE_MESSAGE)
    expect(editor.busy.value).toBe(false)
  })

  it('refuses a year that is not four digits without any request', async () => {
    const server = serverFor()
    const { editor } = await ready(server)
    if (editor.form.value) editor.form.value.year = '20x9'

    await editor.save()

    expect(server.callsTo(SAVED_KEY)).toBe(0)
    expect(editor.errorOf('year')).toBeDefined()
    expect(editor.notice.value?.text).toBe(INVALID_YEAR_MESSAGE)
  })

  it('reports a saved post that has no caption to apply', async () => {
    const server = serverFor({ [SAVED_KEY]: () => jsonResponse(savedDetail({ caption: null })) })
    const { editor } = await ready(server)

    await editor.save()

    expect(server.callsTo(APPLY_KEY)).toBe(0)
    expect(editor.notice.value?.text).toBe(NOTHING_TO_APPLY_MESSAGE)
  })

  it('sends one save however often the button is pressed', async () => {
    const server = serverFor()
    const { editor } = await ready(server)

    await Promise.all([editor.save(), editor.save(), editor.save()])

    expect(server.callsTo(SAVED_KEY)).toBe(1)
  })

  it('does nothing for a post that cannot be edited', async () => {
    const server = editorServer(
      makeDetail({ editable: false, not_editable_reason: 'Пост зараз обробляється.' }),
    )
    const { editor } = await ready(server)

    await editor.save()

    expect(editor.editable.value).toBe(false)
    expect(server.callsTo(SAVED_KEY)).toBe(0)
  })
})

describe('a published post', () => {
  const published = (overrides = {}) =>
    makeDetail({
      published_at: '2026-10-03T10:00:00Z',
      stored_caption_matches: false,
      ...overrides,
    })

  function serverFor(extra = {}) {
    return editorServer(published(), {
      [SAVED_KEY]: () =>
        jsonResponse(published({ title: 'Нова', caption: makeCaption({ sha256: SHA }) })),
      [APPLY_KEY]: () => appliedReply(),
      ...extra,
    })
  }

  it('is saved without touching the channel', async () => {
    const server = serverFor()
    const { editor } = await ready(server)

    await editor.save()

    expect(server.callsTo(SAVED_KEY)).toBe(1)
    expect(server.callsTo(APPLY_KEY)).toBe(0)
    expect(editor.notice.value?.kind).toBe('info')
    expect(editor.canApplyToChannel.value).toBe(true)
  })

  it('applies to the channel only after the exact confirmation is accepted', async () => {
    const server = serverFor()
    const deps = makeDeps(true)
    const { editor } = await ready(server, deps)

    await editor.applyToChannel()

    expect(deps.confirm).toHaveBeenCalledWith(CONFIRM_APPLY_TEXT)
    expect(CONFIRM_APPLY_TEXT).toBe(
      'Змінити підпис опублікованого допису в каналі? Підписники побачать нову версію.',
    )
    expect(server.callsTo(APPLY_KEY)).toBe(1)
    expect(editor.notice.value?.kind).toBe('success')
  })

  it('does not apply when the confirmation is declined', async () => {
    const server = serverFor()
    const { editor } = await ready(server, makeDeps(false))

    await editor.applyToChannel()

    expect(server.callsTo(APPLY_KEY)).toBe(0)
  })

  it('does not offer the channel button while the stored caption already matches', async () => {
    const server = editorServer(published({ stored_caption_matches: true }))
    const { editor } = await ready(server)

    expect(editor.canApplyToChannel.value).toBe(false)
  })

  it('does not offer the channel button for a post that was never published', async () => {
    const { editor } = await ready(editorServer(makeDetail({ stored_caption_matches: false })))

    expect(editor.canApplyToChannel.value).toBe(false)
  })

  it('archives after a missing channel message and a confirmation', async () => {
    const server = serverFor({
      [APPLY_KEY]: () => errorResponse(409, 'channel_message_missing', 'Повідомлення видалено.'),
      [ARCHIVE_KEY]: () => jsonResponse({ post_id: 7, status_label: 'Архівовано' }),
    })
    const deps = makeDeps(true)
    const { editor } = await ready(server, deps)
    await editor.applyToChannel()

    await editor.archive()

    expect(deps.confirm).toHaveBeenLastCalledWith(CONFIRM_ARCHIVE_TEXT)
    expect(server.callsTo(ARCHIVE_KEY)).toBe(1)
    expect(editor.archived.value).toBe(true)
    expect(editor.channelMissing.value).toBe(false)
    expect(editor.editable.value).toBe(false)
    expect(editor.notice.value?.text).toContain('Архівовано')
  })

  it('never archives before the channel message was reported missing', async () => {
    const server = serverFor({
      [ARCHIVE_KEY]: () => jsonResponse({ post_id: 7, status_label: 'x' }),
    })
    const { editor } = await ready(server)

    await editor.archive()

    expect(server.callsTo(ARCHIVE_KEY)).toBe(0)
  })

  it('does not archive when the confirmation is declined', async () => {
    const server = serverFor({
      [APPLY_KEY]: () => errorResponse(409, 'channel_message_missing', 'Повідомлення видалено.'),
      [ARCHIVE_KEY]: () => jsonResponse({ post_id: 7, status_label: 'x' }),
    })
    const deps = makeDeps(true)
    const { editor } = await ready(server, deps)
    await editor.applyToChannel()
    deps.confirm.mockResolvedValue(false)

    await editor.archive()

    expect(server.callsTo(ARCHIVE_KEY)).toBe(0)
    expect(editor.archived.value).toBe(false)
  })

  it('keeps the post and shows the sentence when the server finds the message alive', async () => {
    const server = serverFor({
      [APPLY_KEY]: () => errorResponse(409, 'channel_message_missing', 'Повідомлення видалено.'),
      [ARCHIVE_KEY]: () =>
        errorResponse(409, 'channel_message_exists', 'Повідомлення в каналі існує.'),
    })
    const { editor } = await ready(server)
    await editor.applyToChannel()

    await editor.archive()

    expect(editor.archived.value).toBe(false)
    expect(editor.notice.value).toEqual({ kind: 'error', text: 'Повідомлення в каналі існує.' })
  })
})

describe('publishing the cover without a video', () => {
  const COVER_KEY = 'POST /api/posts/7/publish-cover'
  const allowed = { allowed: true, reason: null }
  const started = () => jsonResponse({ post_id: 7, status_label: 'Публікується' })

  function serverFor(overrides = {}, extra = {}) {
    return editorServer(makeDetail({ cover_publication: allowed, ...overrides }), {
      [COVER_KEY]: started,
      ...extra,
    })
  }

  it('is offered only when the server allows it, the stored caption matches and nothing is unsaved', async () => {
    const { editor } = await ready(serverFor())
    expect(editor.canPublishCover.value).toBe(true)

    const denied = await ready(
      serverFor({ cover_publication: { allowed: false, reason: 'Вже є відео.' } }),
    )
    expect(denied.editor.coverAllowed.value).toBe(false)
    expect(denied.editor.canPublishCover.value).toBe(false)

    const stale = await ready(serverFor({ stored_caption_matches: false }))
    expect(stale.editor.coverAllowed.value).toBe(true)
    expect(stale.editor.canPublishCover.value).toBe(false)
  })

  it('is held back by an unsaved edit', async () => {
    const { editor } = await ready(serverFor())

    if (editor.form.value) editor.form.value.title = 'Інша'

    expect(editor.coverAllowed.value).toBe(true)
    expect(editor.canPublishCover.value).toBe(false)
  })

  it('asks for the exact confirmation, then sends the stamp the server gave', async () => {
    const stamp = '2026-10-09T12:34:56.123456Z'
    const server = serverFor({ expected_post_updated_at: stamp })
    const { editor, deps } = await ready(server)

    await editor.publishWithoutVideo()

    expect(deps.confirm).toHaveBeenCalledWith(CONFIRM_COVER_TEXT)
    const sent = server.requests.findIndex((request) => request.url.endsWith('/publish-cover'))
    expect(server.bodies[sent]).toEqual({ expected_post_updated_at: stamp })
    expect(server.callsTo(COVER_KEY)).toBe(1)
    expect(editor.notice.value).toEqual({ kind: 'success', text: COVER_PUBLISHING_MESSAGE })
  })

  it('reloads the post after a successful request', async () => {
    const server = serverFor()
    const { editor } = await ready(server)
    const reads = server.callsTo('GET /api/posts/7')

    await editor.publishWithoutVideo()

    expect(server.callsTo('GET /api/posts/7')).toBe(reads + 1)
  })

  it('sends nothing when the confirmation is declined or the cover cannot go', async () => {
    const declined = serverFor()
    const first = await ready(declined, makeDeps(false))
    await first.editor.publishWithoutVideo()
    expect(declined.callsTo(COVER_KEY)).toBe(0)

    const denied = serverFor({ cover_publication: { allowed: false, reason: 'x' } })
    const second = await ready(denied)
    await second.editor.publishWithoutVideo()
    expect(denied.callsTo(COVER_KEY)).toBe(0)
    expect(second.deps.confirm).not.toHaveBeenCalled()
  })

  it.each(['stale_data', 'not_publishable'])(
    'reloads and shows the sentence on %s',
    async (code) => {
      const server = serverFor(
        {},
        { [COVER_KEY]: () => errorResponse(409, code, 'Пост змінився.') },
      )
      const { editor } = await ready(server)
      const reads = server.callsTo('GET /api/posts/7')

      await editor.publishWithoutVideo()

      expect(server.callsTo('GET /api/posts/7')).toBe(reads + 1)
      expect(editor.notice.value).toEqual({ kind: 'error', text: 'Пост змінився.' })
    },
  )

  it('shows every other sentence as it came, without a reload', async () => {
    const server = serverFor(
      {},
      {
        [COVER_KEY]: () =>
          errorResponse(409, 'caption_not_applied', 'Зміни ще не застосовано до чернетки.'),
      },
    )
    const { editor } = await ready(server)
    const reads = server.callsTo('GET /api/posts/7')

    await editor.publishWithoutVideo()

    expect(server.callsTo('GET /api/posts/7')).toBe(reads)
    expect(editor.notice.value?.text).toBe('Зміни ще не застосовано до чернетки.')
    expect(editor.busy.value).toBe(false)
  })
})

describe('back to automatic', () => {
  const savedReply = () => jsonResponse(savedDetail())

  function serverFor() {
    return editorServer(makeDetail(), {
      [SAVED_KEY]: savedReply,
      [APPLY_KEY]: () => appliedReply(),
    })
  }

  function sentRevert(server: FakeServer): string[] {
    const body = server.bodies[server.requests.findIndex((r) => r.method === 'PUT')] as {
      revert_to_automatic: string[]
    }
    return body.revert_to_automatic
  }

  it('sends the marked manual field', async () => {
    const server = serverFor()
    const { editor } = await ready(server)
    editor.toggleRevert('year')

    await editor.save()

    expect(sentRevert(server)).toEqual(['year'])
  })

  it('ignores a field that is not manual', async () => {
    const server = serverFor()
    const { editor } = await ready(server)
    editor.toggleRevert('title')

    await editor.save()

    expect(sentRevert(server)).toEqual([])
  })

  it('takes the mark off on a second press', async () => {
    const server = serverFor()
    const { editor } = await ready(server)
    editor.toggleRevert('year')
    editor.toggleRevert('year')

    await editor.save()

    expect(sentRevert(server)).toEqual([])
  })

  it('lets a new edit of the field win over the mark', async () => {
    const server = serverFor()
    const { editor } = await ready(server)
    editor.toggleRevert('year')
    if (editor.form.value) editor.form.value.year = '2021'

    await editor.save()

    expect(sentRevert(server)).toEqual([])
  })
})

describe('the YouTube Music warning', () => {
  it('appears for a changed link on a post that already has a video', async () => {
    const { editor } = await ready(editorServer(makeDetail({ has_video: true })))
    const row = editor.form.value?.links.find((link) => link.platform === 'youtubeMusic')
    if (row) row.url = 'https://music.youtube.com/watch?v=dQw4w9WgXcQ'

    expect(editor.warning.value).toContain('вже є відео')
  })

  it('stays away while the link is unchanged', async () => {
    const { editor } = await ready(editorServer(makeDetail({ has_video: true })))

    expect(editor.warning.value).toBeNull()
  })

  it('stays away for a post without a video', async () => {
    const { editor } = await ready(editorServer(makeDetail({ has_video: false })))
    const row = editor.form.value?.links.find((link) => link.platform === 'youtubeMusic')
    if (row) row.url = 'https://music.youtube.com/watch?v=dQw4w9WgXcQ'

    expect(editor.warning.value).toBeNull()
  })
})

describe('reloadIfClean', () => {
  it('reloads the post when the form has no unsaved edits', async () => {
    const server = editorServer(makeDetail())
    const { editor } = await ready(server)

    expect(await editor.reloadIfClean()).toBe(true)

    expect(server.callsTo('GET /api/posts/7')).toBe(2)
  })

  it('leaves unsaved edits alone and does not even ask the server', async () => {
    const server = editorServer(makeDetail())
    const { editor } = await ready(server)
    if (editor.form.value === null) throw new Error('form missing')
    editor.form.value.title = 'Моя назва'

    expect(await editor.reloadIfClean()).toBe(false)

    expect(server.callsTo('GET /api/posts/7')).toBe(1)
    expect(editor.form.value.title).toBe('Моя назва')
  })
})

describe('dispose', () => {
  it('cancels a preview that has not been sent yet', async () => {
    const server = editorServer(makeDetail())
    const { editor } = await ready(server)

    editor.dispose()
    await vi.advanceTimersByTimeAsync(100)

    expect(server.callsTo('POST /api/posts/7/preview')).toBe(0)
  })
})
