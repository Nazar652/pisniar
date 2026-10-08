import { describe, expect, it, vi } from 'vitest'
import { postIdFromSearch, useNavigation } from '../src/app/useNavigation'
import { applyColorScheme, useTheme, type ThemeRoot } from '../src/app/useTheme'
import { failureOf, NETWORK_FAILURE, outcomeOf } from '../src/api/outcome'
import { inputValue } from '../src/editor/dom'
import { displaySegments, droppedNotes, lengthLabel, safeHref } from '../src/editor/caption'
import { formatDate, joinArtists } from '../src/posts/format'
import { PAGE_SIZE, usePostList } from '../src/posts/usePostList'
import { applyMainButton, confirmWith, hapticWith } from '../src/telegram/actions'
import { errorResponse, fakeServer, jsonResponse, makeCaption } from './support'

describe('postIdFromSearch', () => {
  it.each([
    ['?post_id=7', 7],
    ['post_id=42', 42],
    ['?x=1&post_id=9', 9],
  ])('reads %s', (search, expected) => {
    expect(postIdFromSearch(search)).toBe(expected)
  })

  it.each([
    '',
    '?post_id=',
    '?post_id=0',
    '?post_id=-1',
    '?post_id=abc',
    '?post_id=1.5',
    '?post_id=01',
    '?post_id=9223372036854775808',
    '?post_id=99999999999999999999',
    '?other=7',
  ])('opens the list for %j', (search) => {
    expect(postIdFromSearch(search)).toBeNull()
  })
})

describe('useNavigation', () => {
  it('starts in the editor for a post id and in the list otherwise', () => {
    expect(useNavigation('?post_id=7').view.value).toEqual({ kind: 'editor', postId: 7 })
    expect(useNavigation('').view.value).toEqual({ kind: 'list' })
  })

  it('moves between the list and an editor', () => {
    const navigation = useNavigation('')

    navigation.openPost(12)
    expect(navigation.inEditor.value).toBe(true)
    expect(navigation.view.value).toEqual({ kind: 'editor', postId: 12 })
    navigation.openList()
    expect(navigation.inEditor.value).toBe(false)
  })
})

describe('theme', () => {
  const fakeWebApp = (scheme: 'light' | 'dark') => {
    const handlers: Record<string, () => void> = {}
    const webApp = {
      colorScheme: scheme,
      onEvent: vi.fn((name: string, handler: () => void) => (handlers[name] = handler)),
      offEvent: vi.fn(),
    }
    return { webApp: webApp as unknown as TelegramWebApp, handlers }
  }

  it('mirrors the scheme and defaults to light outside Telegram', () => {
    const root: ThemeRoot = { dataset: {} }

    applyColorScheme(undefined, root)
    expect(root.dataset['colorScheme']).toBe('light')
    applyColorScheme(fakeWebApp('dark').webApp, root)
    expect(root.dataset['colorScheme']).toBe('dark')
  })

  it('follows themeChanged and stops after the unsubscribe call', () => {
    const root: ThemeRoot = { dataset: {} }
    const { webApp, handlers } = fakeWebApp('light')

    const stop = useTheme(webApp, root)
    ;(webApp as { colorScheme: string }).colorScheme = 'dark'
    handlers['themeChanged']?.()

    expect(root.dataset['colorScheme']).toBe('dark')
    stop()
    expect(webApp.offEvent).toHaveBeenCalledWith('themeChanged', handlers['themeChanged'])
  })

  it('has nothing to subscribe to outside Telegram', () => {
    expect(() => useTheme(undefined, { dataset: {} })()).not.toThrow()
  })
})

describe('outcomeOf and failureOf', () => {
  it('returns the data of an answer', async () => {
    const outcome = await outcomeOf(
      Promise.resolve({ data: { user_id: 1 }, response: new Response() }),
    )

    expect(outcome).toEqual({ ok: true, data: { user_id: 1 } })
  })

  it('keeps status, code, sentence and field errors of an error answer', async () => {
    const body = {
      code: 'invalid_fields',
      message: 'Погано.',
      errors: [{ field: 'year', message: 'Рік.' }, { nope: 1 }, 'x'],
    }

    const outcome = await outcomeOf(
      Promise.resolve({ error: body, response: new Response(null, { status: 422 }) }),
    )

    expect(outcome).toEqual({
      ok: false,
      failure: {
        status: 422,
        code: 'invalid_fields',
        message: 'Погано.',
        fieldErrors: [{ field: 'year', message: 'Рік.' }],
      },
    })
  })

  it('turns a rejected request into the network failure', async () => {
    const outcome = await outcomeOf(Promise.reject(new TypeError('offline')))

    expect(outcome).toEqual({ ok: false, failure: NETWORK_FAILURE })
  })

  it('falls back to a status sentence and an unknown code for a body without them', () => {
    const failure = failureOf(503, undefined)

    expect(failure.code).toBe('unknown')
    expect(failure.message).toContain('недоступний')
    expect(failure.fieldErrors).toEqual([])
    expect(failureOf(422, { errors: 'x' }).fieldErrors).toEqual([])
  })
})

describe('usePostList', () => {
  const summary = (id: number) => ({
    id,
    status_label: 'Чернетка готова',
    title: `Пост ${id}`,
    artists: ['А'],
    created_at: '2026-10-01T10:00:00Z',
    published_at: null,
    editable: true,
    not_editable_reason: null,
    has_video: false,
  })

  it('loads the first page and keeps the cursor', async () => {
    const server = fakeServer({
      'GET /api/posts': () => jsonResponse({ items: [summary(9), summary(8)], next_before_id: 8 }),
    })
    const list = usePostList(server.client)

    await list.load()

    expect(list.status.value).toBe('loaded')
    expect(list.items.value.map((item) => item.id)).toEqual([9, 8])
    expect(list.nextBeforeId.value).toBe(8)
    expect(new URL(server.requests[0]?.url ?? '').searchParams.get('limit')).toBe(String(PAGE_SIZE))
  })

  it('asks for the next page by the cursor and appends it without repeats', async () => {
    const pages = [
      { items: [summary(9), summary(8)], next_before_id: 8 },
      { items: [summary(8), summary(7)], next_before_id: null },
    ]
    const server = fakeServer({ 'GET /api/posts': () => jsonResponse(pages.shift()) })
    const list = usePostList(server.client)
    await list.load()

    await list.loadMore()

    expect(new URL(server.requests[1]?.url ?? '').searchParams.get('before_id')).toBe('8')
    expect(list.items.value.map((item) => item.id)).toEqual([9, 8, 7])
    expect(list.nextBeforeId.value).toBeNull()
  })

  it('has nothing more to load without a cursor', async () => {
    const server = fakeServer({
      'GET /api/posts': () => jsonResponse({ items: [], next_before_id: null }),
    })
    const list = usePostList(server.client)
    await list.load()

    await list.loadMore()

    expect(server.requests).toHaveLength(1)
  })

  it('does not start a second page while one is loading', async () => {
    let release: (response: Response) => void = () => undefined
    let calls = 0
    const server = fakeServer({
      'GET /api/posts': () => {
        calls += 1
        if (calls === 1) return jsonResponse({ items: [summary(9)], next_before_id: 9 })
        return new Promise<Response>((resolve) => (release = resolve))
      },
    })
    const list = usePostList(server.client)
    await list.load()

    const first = list.loadMore()
    await vi.waitFor(() => expect(calls).toBe(2))
    await list.loadMore()
    release(jsonResponse({ items: [summary(8)], next_before_id: null }))
    await first

    expect(server.requests).toHaveLength(2)
  })

  it('shows the sentence of a failed first page and can try again', async () => {
    let fail = true
    const server = fakeServer({
      'GET /api/posts': () =>
        fail
          ? errorResponse(503, 'database_unavailable', 'База недоступна.')
          : jsonResponse({ items: [summary(1)], next_before_id: null }),
    })
    const list = usePostList(server.client)

    await list.load()
    expect([list.status.value, list.message.value]).toEqual(['failed', 'База недоступна.'])
    fail = false
    await list.load()

    expect([list.status.value, list.message.value]).toEqual(['loaded', ''])
  })

  it('keeps the shown cards when the next page fails', async () => {
    let calls = 0
    const server = fakeServer({
      'GET /api/posts': () =>
        ++calls === 1
          ? jsonResponse({ items: [summary(9)], next_before_id: 9 })
          : errorResponse(503, 'database_unavailable', 'База недоступна.'),
    })
    const list = usePostList(server.client)
    await list.load()

    await list.loadMore()

    expect(list.items.value).toHaveLength(1)
    expect(list.nextBeforeId.value).toBe(9)
    expect(list.message.value).toBe('База недоступна.')
    expect(list.loadingMore.value).toBe(false)
  })
})

describe('caption helpers', () => {
  it('turns segments into text, web links and breaks', () => {
    const segments = displaySegments([
      { kind: 'text', text: 'Тема', href: null },
      { kind: 'line_break', text: null, href: null },
      { kind: 'link', text: 'Spotify', href: 'https://open.spotify.com/x' },
    ])

    expect(segments).toEqual([
      { kind: 'text', text: 'Тема' },
      { kind: 'break' },
      { kind: 'link', text: 'Spotify', href: 'https://open.spotify.com/x' },
    ])
  })

  it.each(['javascript:alert(1)', 'data:text/html,x', 'not a url', '', 'ftp://x'])(
    'shows %j as plain text, never as a link',
    (href) => {
      expect(displaySegments([{ kind: 'link', text: 'x', href }])).toEqual([
        { kind: 'text', text: 'x' },
      ])
    },
  )

  it('accepts http and https only', () => {
    expect(safeHref('http://a.test')).toBe('http://a.test')
    expect(safeHref('https://a.test')).toBe('https://a.test')
    expect(safeHref(null)).toBeNull()
  })

  it('shows the length against the limit', () => {
    expect(lengthLabel(makeCaption({ visible_length: 987 }))).toBe('987/1024')
  })

  it('lists everything that did not fit, then the warnings', () => {
    const notes = droppedNotes(
      makeCaption({
        dropped_genres: ['Рок'],
        dropped_platforms: ['tidal'],
        release_title_dropped: true,
        warnings: ['Увага.'],
      }),
    )

    expect(notes).toEqual([
      'Жанри не влізли: Рок.',
      'Платформи не влізли: tidal.',
      'Назва релізу не влізла.',
      'Увага.',
    ])
    expect(droppedNotes(makeCaption())).toEqual([])
  })
})

describe('formatting and dom helpers', () => {
  it('formats a date and gives nothing for a missing or broken one', () => {
    expect(formatDate('2026-10-05T14:30:00Z', 'en-US')).toContain('Oct')
    expect(formatDate(null)).toBe('')
    expect(formatDate('garbage')).toBe('')
  })

  it('joins artists or names the absence', () => {
    expect(joinArtists(['А', 'Б'])).toBe('А, Б')
    expect(joinArtists([])).toBe('Без артиста')
  })

  it('reads the value of a field from its event and is empty for anything else', () => {
    class FakeInput {
      value = 'x'
    }
    vi.stubGlobal('HTMLInputElement', FakeInput)
    vi.stubGlobal('HTMLSelectElement', function Select() {})
    vi.stubGlobal('HTMLTextAreaElement', function TextArea() {})

    expect(inputValue({ target: new FakeInput() } as unknown as Event)).toBe('x')
    expect(inputValue({ target: {} } as unknown as Event)).toBe('')
    vi.unstubAllGlobals()
  })
})

describe('Telegram actions', () => {
  it('resolves a confirmation with the answer of showConfirm', async () => {
    const webApp = {
      showConfirm: vi.fn((_message: string, callback: (value: boolean) => void) => callback(true)),
    } as unknown as TelegramWebApp

    expect(await confirmWith(webApp)('Точно?')).toBe(true)
    expect(webApp.showConfirm).toHaveBeenCalledWith('Точно?', expect.any(Function))
  })

  it('treats a declined confirmation and a missing SDK as no', async () => {
    const declined = {
      showConfirm: (_message: string, callback: (value: boolean) => void) => callback(false),
    } as unknown as TelegramWebApp

    expect(await confirmWith(declined)('?')).toBe(false)
    expect(await confirmWith(undefined)('?')).toBe(false)
  })

  it('sends haptic feedback when the client has it', () => {
    const notificationOccurred = vi.fn()
    const webApp = { HapticFeedback: { notificationOccurred } } as unknown as TelegramWebApp

    hapticWith(webApp)('success')
    hapticWith(undefined)('error')
    hapticWith({} as TelegramWebApp)('error')

    expect(notificationOccurred).toHaveBeenCalledExactlyOnceWith('success')
  })

  it('maps the wanted state onto the button calls', () => {
    const button = {
      text: '',
      show: vi.fn(),
      hide: vi.fn(),
      enable: vi.fn(),
      disable: vi.fn(),
      showProgress: vi.fn(),
      hideProgress: vi.fn(),
    } as unknown as TelegramMainButton

    applyMainButton(button, { text: 'Зберегти', visible: true, enabled: true, busy: false })
    expect(button.text).toBe('Зберегти')
    expect(button.enable).toHaveBeenCalled()
    expect(button.hideProgress).toHaveBeenCalled()
    expect(button.show).toHaveBeenCalled()

    applyMainButton(button, { text: 'Зберегти', visible: false, enabled: false, busy: true })
    expect(button.disable).toHaveBeenCalled()
    expect(button.showProgress).toHaveBeenCalledWith(false)
    expect(button.hide).toHaveBeenCalled()
  })
})
