import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { describe, expect, it, vi } from 'vitest'
import FragmentEditor from '../src/fragment/FragmentEditor.vue'
import FragmentSection from '../src/fragment/FragmentSection.vue'
import { useFragmentEditor } from '../src/fragment/useFragmentEditor'
import { lockVerticalSwipes } from '../src/telegram/actions'
import { errorResponse, fakeServer, jsonResponse, makeFragmentView } from './support'

async function render(component: object, props: Record<string, unknown>): Promise<string> {
  return renderToString(createSSRApp(component, props))
}

async function loadedEditor(overrides = {}) {
  const server = fakeServer({
    'GET /api/posts/7/fragment': () => jsonResponse(makeFragmentView(overrides)),
    'POST /api/posts/7/fragment': () =>
      jsonResponse({ post_id: 7, status_label: 'Рендериться', start_ms: 0, end_ms: 45_000 }),
  })
  const editor = useFragmentEditor(server.client, 7)
  await editor.load()
  return { editor, server }
}

describe('FragmentEditor', () => {
  it('shows a loading line before the view is there', async () => {
    const server = fakeServer({})
    const editor = useFragmentEditor(server.client, 7)

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).toContain('Завантаження')
    expect(html).not.toContain('Зробити відео')
    expect(server.requests).toHaveLength(0)
  })

  it('shows the bounds, the length, the nudges and its own action button', async () => {
    const { editor } = await loadedEditor({ start_ms: 12_300, end_ms: 57_300 })

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).toContain('Початок: 0:12.3')
    expect(html).toContain('Кінець: 0:57.3')
    expect(html).toContain('Довжина: 0:45.0')
    expect(html).toContain('не більше 2:00.0 із 3:20.0')
    for (const label of ['−1 с', '−0,1 с', '+0,1 с', '+1 с']) {
      expect(html.split(label)).toHaveLength(3)
    }
    expect(html).toContain('Програти фрагмент')
    expect(html).toContain('Зробити відео з цим фрагментом')
    expect(html).toContain('class="fragment__wave"')
    expect(html).toContain('<audio')
  })

  it('offers the fragment, the whole track and an edge at the playhead', async () => {
    const { editor } = await loadedEditor()

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).toContain('Програти фрагмент')
    expect(html).toContain('Грати трек')
    expect(html).toContain('Початок тут')
    expect(html).toContain('Кінець тут')
  })

  it('keeps the edge-at-playhead buttons off until the audio has loaded', async () => {
    const { editor } = await loadedEditor()

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html.match(/fragment__here"[^>]*disabled/g)).toHaveLength(2)
  })

  it('keeps the play button off until the waveform has made its region', async () => {
    const { editor } = await loadedEditor()

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).toMatch(/fragment__play"[^>]*disabled/)
  })

  it('says why the waveform is flat when the server has no peaks', async () => {
    const withPeaks = await loadedEditor()
    const without = await loadedEditor({ peaks: null })

    const plain = await render(FragmentEditor, { editor: withPeaks.editor, webApp: undefined })
    const flat = await render(FragmentEditor, { editor: without.editor, webApp: undefined })

    expect(plain).not.toContain('Хвилю для цього аудіо не пораховано')
    expect(flat).toContain('Хвилю для цього аудіо не пораховано')
  })

  it('writes colours and sizes through classes, never through style strings or html injection', async () => {
    const { editor } = await loadedEditor()

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).not.toContain('style=')
    expect(html).not.toContain('<script')
  })

  it('tells the admin the new preview will arrive and drops the action button once submitted', async () => {
    const { editor } = await loadedEditor()
    await editor.submit()

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).toContain('Нове прев’ю з’явиться в чаті')
    expect(html).toContain('notice--success')
    expect(html).not.toContain('Зробити відео з цим фрагментом')
  })

  it('shows the server sentence of a refusal as an alert and keeps the editor usable', async () => {
    const server = fakeServer({
      'GET /api/posts/7/fragment': () => jsonResponse(makeFragmentView()),
      'POST /api/posts/7/fragment': () =>
        errorResponse(422, 'invalid_fragment', 'Фрагмент не підходить: довший за 2:00.'),
    })
    const editor = useFragmentEditor(server.client, 7)
    await editor.load()
    await editor.submit()

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).toContain('Фрагмент не підходить: довший за 2:00.')
    expect(html).toContain('role="alert"')
    expect(html).toMatch(/fragment__submit"[^>]*>/)
    expect(html).not.toMatch(/fragment__submit"[^>]*disabled/)
  })

  it('offers a retry when the view could not be loaded', async () => {
    const server = fakeServer({
      'GET /api/posts/7/fragment': () =>
        errorResponse(409, 'audio_missing', 'Аудіо ще не завантажено.'),
    })
    const editor = useFragmentEditor(server.client, 7)
    await editor.load()

    const html = await render(FragmentEditor, { editor, webApp: undefined })

    expect(html).toContain('Аудіо ще не завантажено.')
    expect(html).toContain('Спробувати знову')
    expect(html).not.toContain('<audio')
  })
})

describe('FragmentSection', () => {
  const fragment = {
    start_ms: 0,
    end_ms: 45_000,
    choosable: true,
    preparable: false,
    preparing: false,
    reason: null,
  }
  const unavailable = { start_ms: null, end_ms: null, choosable: false }
  const base = {
    client: fakeServer({}).client,
    postId: 7,
    postStamp: '2026-05-01T12:00:00Z',
    webApp: undefined,
  }

  it('offers to prepare the audio of a draft with the warning about the cover', async () => {
    const html = await render(FragmentSection, {
      ...base,
      fragment: {
        ...unavailable,
        preparable: true,
        preparing: false,
        reason: 'Спершу підготуємо аудіо.',
      },
    })

    expect(html).toContain('Спершу підготуємо аудіо.')
    expect(html).toContain('Обрати фрагмент')
    expect(html).not.toContain('Готуємо аудіо')
  })

  it('says the audio is being prepared and offers no button while it is', async () => {
    const html = await render(FragmentSection, {
      ...base,
      fragment: { ...unavailable, preparable: false, preparing: true, reason: 'Аудіо готується.' },
    })

    expect(html).toContain('Готуємо аудіо…')
    expect(html).toContain('role="status"')
    expect(html).not.toContain('<button')
  })

  it('offers to give up ready audio with a quiet button and no confirmation', async () => {
    const html = await render(FragmentSection, {
      ...base,
      fragment: { ...fragment, cancellable: true },
    })

    expect(html).toContain('Скасувати підготовку')
    expect(html).toMatch(/btn btn--quiet"[^>]*>\s*Скасувати підготовку/)
    expect(html).not.toMatch(/btn btn--quiet"[^>]*disabled/)
  })

  it('offers no cancellation for anything but ready audio', async () => {
    const html = await render(FragmentSection, {
      ...base,
      fragment: { ...fragment, cancellable: false },
    })

    expect(html).not.toContain('Скасувати підготовку')
  })

  it('renders every server sentence as text, never as markup', async () => {
    const html = await render(FragmentSection, {
      ...base,
      fragment: {
        ...unavailable,
        preparable: true,
        preparing: false,
        reason: '<img src=x onerror=alert(1)>',
      },
    })

    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })

  it('offers to open the editor and shows the bounds in the video now', async () => {
    const html = await render(FragmentSection, { ...base, fragment })

    expect(html).toContain('Фрагмент')
    expect(html).toContain('Зараз у відео: 0:00.0 – 0:45.0')
    expect(html).toContain('Обрати фрагмент')
    expect(html).not.toContain('Завантаження')
  })

  it('shows the reason instead of a button when the fragment cannot be chosen', async () => {
    const html = await render(FragmentSection, {
      ...base,
      fragment: {
        ...unavailable,
        preparable: false,
        preparing: false,
        reason: 'Відео ще не готове.',
      },
    })

    expect(html).toContain('Відео ще не готове.')
    expect(html).not.toContain('Обрати фрагмент')
  })

  it('has a sentence of its own when the server gave no reason', async () => {
    const html = await render(FragmentSection, {
      ...base,
      fragment: { ...unavailable, preparable: false, preparing: false, reason: null },
    })

    expect(html).toContain('Фрагмент зараз обрати не можна.')
  })
})

describe('lockVerticalSwipes', () => {
  function fakeWebApp(version: string, atLeast: boolean) {
    return {
      version,
      isVersionAtLeast: vi.fn((required: string) => required === '7.7' && atLeast),
      disableVerticalSwipes: vi.fn(),
      enableVerticalSwipes: vi.fn(),
    }
  }

  it('disables the swipes on a client that knows how and enables them again on release', () => {
    const webApp = fakeWebApp('8.0', true)

    const release = lockVerticalSwipes(webApp as unknown as TelegramWebApp)

    expect(webApp.isVersionAtLeast).toHaveBeenCalledWith('7.7')
    expect(webApp.disableVerticalSwipes).toHaveBeenCalledOnce()
    expect(webApp.enableVerticalSwipes).not.toHaveBeenCalled()
    release()
    expect(webApp.enableVerticalSwipes).toHaveBeenCalledOnce()
  })

  it('touches nothing on an older client', () => {
    const webApp = fakeWebApp('7.0', false)

    const release = lockVerticalSwipes(webApp as unknown as TelegramWebApp)
    release()

    expect(webApp.disableVerticalSwipes).not.toHaveBeenCalled()
    expect(webApp.enableVerticalSwipes).not.toHaveBeenCalled()
  })

  it('does nothing outside Telegram', () => {
    expect(() => lockVerticalSwipes(undefined)()).not.toThrow()
  })
})
