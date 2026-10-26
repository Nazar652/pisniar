import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { describe, expect, it, vi } from 'vitest'
import { usePostEditor } from '../src/editor/usePostEditor'
import FragmentEditor from '../src/fragment/FragmentEditor.vue'
import FragmentSection from '../src/fragment/FragmentSection.vue'
import {
  SAVED_MESSAGE,
  SAVED_STALE_MESSAGE,
  useFragmentEditor,
} from '../src/fragment/useFragmentEditor'
import { useLyricsEditor } from '../src/lyrics/useLyricsEditor'
import {
  editorServer,
  errorResponse,
  fakeServer,
  jsonResponse,
  makeDetail,
  makeFragmentView,
  makeLyricsView,
  MICRO_STAMP,
  type FakeServer,
  type Handler,
} from './support'

const GET = 'GET /api/posts/7/fragment'
const PUT = 'PUT /api/posts/7/fragment'
const POST = 'POST /api/posts/7/fragment'
const NEW_STAMP = '2026-10-09T12:40:00.654321Z'

function saved(overrides = {}) {
  return {
    post_id: 7,
    post_updated_at: NEW_STAMP,
    start_ms: 10_000,
    end_ms: 55_000,
    preview_stale: false,
    ...overrides,
  }
}

function serverWith(extra: Record<string, Handler> = {}): FakeServer {
  return fakeServer({
    [GET]: () => jsonResponse(makeFragmentView()),
    [PUT]: () => jsonResponse(saved()),
    [POST]: () =>
      jsonResponse({ post_id: 7, status_label: 'Рендериться', start_ms: 0, end_ms: 45_000 }),
    ...extra,
  })
}

function bodyOf(server: FakeServer, method: string): unknown {
  return server.bodies[server.requests.map((request) => request.method).lastIndexOf(method)]
}

async function moved(
  server: FakeServer,
  onSaved = vi.fn(),
  beforeSubmit = vi.fn(async () => true),
) {
  const editor = useFragmentEditor(server.client, 7, undefined, beforeSubmit, onSaved)
  await editor.load()
  editor.nudgeEdge('start', 10_000)
  editor.nudgeEdge('end', 10_000)
  return { editor, onSaved, beforeSubmit }
}

describe('useFragmentEditor save', () => {
  it('can save only with moved bounds that obey the rules', async () => {
    const server = serverWith()
    const editor = useFragmentEditor(server.client, 7)
    expect(editor.canSave.value).toBe(false)

    await editor.load()
    expect(editor.canSave.value).toBe(false)

    editor.nudgeEdge('end', 1000)
    expect(editor.canSave.value).toBe(true)
  })

  it('sends the bounds with the stamp exactly as given, and is clean after the answer', async () => {
    const server = serverWith()
    const { editor, onSaved } = await moved(server)

    await editor.save()

    expect(bodyOf(server, 'PUT')).toEqual({
      start_ms: 10_000,
      end_ms: 55_000,
      expected_post_updated_at: MICRO_STAMP,
    })
    expect(editor.status.value).toBe('ready')
    expect(editor.dirty.value).toBe(false)
    expect(editor.canSave.value).toBe(false)
    expect(editor.bounds.value).toEqual({ startMs: 10_000, endMs: 55_000 })
    expect(editor.message.value).toBe(SAVED_MESSAGE)
    expect(editor.messageKind.value).toBe('success')
    expect(onSaved).toHaveBeenCalledExactlyOnceWith(saved(), MICRO_STAMP)
  })

  it('keeps the view, so the waveform is not built again', async () => {
    const server = serverWith()
    const { editor } = await moved(server)
    const before = editor.view.value

    await editor.save()

    expect(editor.view.value).toBe(before)
    expect(server.callsTo(GET)).toBe(1)
  })

  it('submits afterwards with the stamp the save returned', async () => {
    const server = serverWith()
    const { editor } = await moved(server)
    await editor.save()

    await editor.submit()

    expect(bodyOf(server, 'POST')).toMatchObject({
      start_ms: 10_000,
      end_ms: 55_000,
      expected_post_updated_at: NEW_STAMP,
    })
  })

  it('says the preview is out of date when the answer says so', async () => {
    const server = serverWith({ [PUT]: () => jsonResponse(saved({ preview_stale: true })) })
    const { editor } = await moved(server)

    await editor.save()

    expect(editor.message.value).toBe(SAVED_STALE_MESSAGE)
    expect(SAVED_STALE_MESSAGE).toContain('зробіть відео заново')
    expect(SAVED_STALE_MESSAGE).not.toContain('опублікується')
  })

  it('does not ask about other unsaved work: only a render does', async () => {
    const server = serverWith()
    const { editor, beforeSubmit } = await moved(server)

    await editor.save()
    expect(beforeSubmit).not.toHaveBeenCalled()

    editor.nudgeEdge('end', 1000)
    await editor.submit()
    expect(beforeSubmit).toHaveBeenCalledOnce()
  })

  it('shows the success sentence only until the bounds move again', async () => {
    const { editor } = await moved(serverWith())
    await editor.save()

    editor.nudgeEdge('end', -1000)

    expect(editor.message.value).toBe('')
    expect(editor.dirty.value).toBe(true)
  })

  it('sends nothing while saving, and drops moves meanwhile', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => (release = resolve))
    const server = serverWith({
      [PUT]: async () => {
        await gate
        return jsonResponse(saved())
      },
    })
    const { editor } = await moved(server)

    const first = editor.save()
    await editor.save()
    editor.nudgeEdge('end', 1000)
    expect(editor.status.value).toBe('saving')
    expect(editor.bounds.value.endMs).toBe(55_000)
    release()
    await first

    expect(server.callsTo(PUT)).toBe(1)
  })

  it('reloads on stale_data and keeps the sentence with the bounds of the server', async () => {
    const views = [makeFragmentView(), makeFragmentView({ start_ms: 1000, end_ms: 40_000 })]
    let served = 0
    const server = serverWith({
      [GET]: () => jsonResponse(views[Math.min(served++, 1)]),
      [PUT]: () => errorResponse(409, 'stale_data', 'Пост змінився.'),
    })
    const { editor, onSaved } = await moved(server)

    await editor.save()

    expect(editor.status.value).toBe('ready')
    expect(editor.messageKind.value).toBe('error')
    expect(editor.message.value).toBe('Пост змінився.')
    expect(editor.bounds.value).toEqual({ startMs: 1000, endMs: 40_000 })
    expect(onSaved).not.toHaveBeenCalled()
  })

  it.each([
    ['post_busy', 'ready'],
    ['invalid_fragment', 'ready'],
    ['not_choosable', 'failed'],
    ['audio_missing', 'failed'],
  ])('handles %s like a refused render: the editor ends %s', async (code, ending) => {
    const server = serverWith({ [PUT]: () => errorResponse(409, code, 'Речення сервера.') })
    const { editor, onSaved } = await moved(server)

    await editor.save()

    expect(editor.status.value).toBe(ending)
    expect(editor.message.value).toBe('Речення сервера.')
    expect(editor.messageKind.value).toBe('error')
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('shows the restarting sentence for a proxy 502 and stays ready', async () => {
    const server = serverWith({
      [PUT]: () => new Response('<html>Bad Gateway</html>', { status: 502 }),
    })
    const { editor } = await moved(server)

    await editor.save()

    expect(editor.status.value).toBe('ready')
    expect(editor.message.value).toBe(
      'Сервер перезапускається або недоступний. Спробуйте за хвилину.',
    )
  })
})

async function render(component: object, props: Record<string, unknown>): Promise<string> {
  return renderToString(createSSRApp(component, props))
}

describe('FragmentEditor save button', () => {
  it('is quiet, off while nothing moved, and on once the bounds moved', async () => {
    const { editor } = await moved(serverWith())
    const clean = useFragmentEditor(serverWith().client, 7)
    await clean.load()

    const off = await render(FragmentEditor, { editor: clean, webApp: undefined })
    const on = await render(FragmentEditor, { editor, webApp: undefined })

    expect(off).toMatch(/btn--quiet fragment__save"[^>]*disabled/)
    expect(on).toMatch(/btn--quiet fragment__save"/)
    expect(on).not.toMatch(/fragment__save"[^>]*disabled/)
    expect(on).toContain('Зберегти фрагмент')
    expect(on).toContain('Зробити відео з цим фрагментом')
  })

  it('shows the success sentence as a success notice', async () => {
    const { editor } = await moved(serverWith())
    await editor.save()

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).toContain('notice--success')
    expect(html).toContain(SAVED_MESSAGE)
  })

  it('shows a refusal as an error notice', async () => {
    const { editor } = await moved(
      serverWith({ [PUT]: () => errorResponse(409, 'post_busy', 'Зайнято.') }),
    )
    await editor.save()

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).toContain('notice--error')
    expect(html).toContain('Зайнято.')
  })
})

describe('FragmentSection stale preview', () => {
  const fragment = {
    start_ms: 10_000,
    end_ms: 55_000,
    choosable: true,
    preparable: false,
    preparing: false,
    cancellable: false,
    preview_stale: false,
    reason: null,
  }
  const base = {
    client: fakeServer({}).client,
    postId: 7,
    postStamp: MICRO_STAMP,
    webApp: undefined,
  }

  it('says what is in the video while the preview is current', async () => {
    const html = await render(FragmentSection, { ...base, fragment })

    expect(html).toContain('Зараз у відео: 0:10.0 – 0:55.0')
    expect(html).not.toContain('застаріло')
  })

  it('names the chosen fragment and warns that the preview is out of date', async () => {
    const html = await render(FragmentSection, {
      ...base,
      fragment: { ...fragment, preview_stale: true },
    })

    expect(html).toContain('Обраний фрагмент: 0:10.0 – 0:55.0')
    expect(html).not.toContain('Зараз у відео')
    expect(html).toContain('Фрагмент змінено — прев’ю застаріло, зробіть відео заново.')
    expect(html).not.toContain('опублікується')
  })
})

describe('usePostEditor adoptFragmentSave', () => {
  function previewedDetail() {
    return makeDetail({
      expected_post_updated_at: MICRO_STAMP,
      fragment: {
        start_ms: 0,
        end_ms: 45_000,
        choosable: true,
        preparable: false,
        preparing: false,
        cancellable: false,
        preview_stale: false,
        reason: null,
      },
    })
  }

  async function ready() {
    const server = editorServer(previewedDetail())
    const editor = usePostEditor(server.client, 7, {
      confirm: async () => true,
      haptic: () => undefined,
      debounceMs: 10,
    })
    await editor.load()
    return { editor, server }
  }

  it('patches the fragment and the stamp and keeps unsaved field edits', async () => {
    const { editor } = await ready()
    if (editor.form.value === null) throw new Error('form missing')
    editor.form.value.title = 'Моя назва'

    editor.adoptFragmentSave(saved({ preview_stale: true }), MICRO_STAMP)

    expect(editor.detail.value?.expected_post_updated_at).toBe(NEW_STAMP)
    expect(editor.detail.value?.fragment).toMatchObject({
      start_ms: 10_000,
      end_ms: 55_000,
      preview_stale: true,
      choosable: true,
    })
    expect(editor.form.value.title).toBe('Моя назва')
    expect(editor.dirty.value).toBe(true)
  })

  it('makes the next field save carry the new stamp', async () => {
    const { editor, server } = await ready()
    if (editor.form.value === null) throw new Error('form missing')
    editor.form.value.title = 'Моя назва'
    editor.adoptFragmentSave(saved(), MICRO_STAMP)
    server.handlers['PUT /api/posts/7/fields'] = () => errorResponse(409, 'stale_data', 'x')

    await editor.save()

    expect(bodyOf(server, 'PUT')).toMatchObject({ expected_post_updated_at: NEW_STAMP })
  })

  it('ignores an answer for a version the post is no longer at', async () => {
    const { editor } = await ready()

    editor.adoptFragmentSave(saved(), '2020-01-01T00:00:00Z')

    expect(editor.detail.value?.expected_post_updated_at).toBe(MICRO_STAMP)
    expect(editor.detail.value?.fragment.start_ms).toBe(0)
  })
})

describe('useLyricsEditor refreshFragment', () => {
  const LYRICS = 'GET /api/posts/7/lyrics'

  async function opened(views: ReturnType<typeof makeFragmentView>[]) {
    let served = 0
    const server = fakeServer({
      [LYRICS]: () => jsonResponse(makeLyricsView()),
      [GET]: () => jsonResponse(views[Math.min(served++, views.length - 1)]),
      [POST]: () =>
        jsonResponse({ post_id: 7, status_label: 'Рендериться', start_ms: 0, end_ms: 45_000 }),
    })
    const editor = useLyricsEditor(server.client, 7, { confirm: async () => true })
    await editor.load()
    return { editor, server }
  }

  it('takes the saved bounds and stamp but keeps the audio url and the text', async () => {
    const { editor, server } = await opened([
      makeFragmentView(),
      makeFragmentView({
        start_ms: 10_000,
        end_ms: 55_000,
        post_updated_at: NEW_STAMP,
        audio_url: '/api/posts/7/audio?token=second',
      }),
    ])
    const lines = editor.lines.value

    expect(await editor.refreshFragment()).toBe(true)

    expect(editor.fragment.value).toMatchObject({ start_ms: 10_000, end_ms: 55_000 })
    expect(editor.audioUrl.value).toBe('/api/posts/7/audio?token=first')
    expect(editor.lines.value).toBe(lines)
    expect(editor.status.value).toBe('ready')

    await editor.save(true)
    expect(bodyOf(server, 'POST')).toEqual({
      start_ms: 10_000,
      end_ms: 55_000,
      expected_post_updated_at: NEW_STAMP,
    })
  })

  it('does nothing before the editor has loaded, and keeps the bounds when the read fails', async () => {
    const idle = useLyricsEditor(serverWith().client, 7, { confirm: async () => true })
    expect(await idle.refreshFragment()).toBe(false)

    const { editor, server } = await opened([makeFragmentView()])
    server.handlers[GET] = () => errorResponse(503, 'database_unavailable', 'Недоступно.')

    expect(await editor.refreshFragment()).toBe(false)
    expect(editor.fragment.value).toMatchObject({ start_ms: 0, end_ms: 45_000 })
  })
})
