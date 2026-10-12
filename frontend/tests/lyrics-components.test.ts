import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { describe, expect, it, vi } from 'vitest'
import { LEAVE_GUARD } from '../src/app/leaveGuard'
import LyricsEditor from '../src/lyrics/LyricsEditor.vue'
import LyricsSection from '../src/lyrics/LyricsSection.vue'
import { releaseEditor } from '../src/lyrics/cleanup'
import { blurAfterClick, blurButton } from '../src/lyrics/focus'
import { attachKeys, isControlTarget, isTextTarget } from '../src/lyrics/keys'
import { useLyricsEditor } from '../src/lyrics/useLyricsEditor'
import { setClosingConfirmation } from '../src/telegram/actions'
import {
  errorResponse,
  fakeServer,
  jsonResponse,
  makeFragmentView,
  makeLyricsView,
  type FakeServer,
  type LyricsView,
} from './support'

const STORED: LyricsView = makeLyricsView({
  lines: [
    { text: 'Перший', start_ms: 1000, source: 'lrclib' },
    { text: 'Другий', start_ms: 20_000, source: 'lrclib' },
    { text: 'Третій', start_ms: null, source: 'manual' },
  ],
})

async function render(component: object, props: Record<string, unknown>): Promise<string> {
  return renderToString(createSSRApp(component, props))
}

function serverWith(view: LyricsView = STORED): FakeServer {
  return fakeServer({
    'GET /api/posts/7/lyrics': () => jsonResponse(view),
    'GET /api/posts/7/fragment': () =>
      jsonResponse(makeFragmentView({ start_ms: 0, end_ms: 10_000 })),
  })
}

async function loadedEditor(view: LyricsView = STORED) {
  const editor = useLyricsEditor(serverWith(view).client, 7, { confirm: async () => true })
  await editor.load()
  return editor
}

describe('LyricsEditor', () => {
  it('shows a loading line before the text is there and asks for nothing', async () => {
    const server = fakeServer({})
    const editor = useLyricsEditor(server.client, 7, { confirm: async () => true })

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toContain('Завантаження')
    expect(html).not.toContain('<audio')
    expect(server.requests).toHaveLength(0)
  })

  it('offers a retry with the server sentence when the text could not be loaded', async () => {
    const server = fakeServer({
      'GET /api/posts/7/lyrics': () =>
        errorResponse(409, 'not_choosable', 'Пост зараз не можна змінити.'),
      'GET /api/posts/7/fragment': () => jsonResponse(makeFragmentView()),
    })
    const editor = useLyricsEditor(server.client, 7, { confirm: async () => true })
    await editor.load()

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toContain('Пост зараз не можна змінити.')
    expect(html).toContain('Спробувати знову')
    expect(html).not.toContain('<audio')
  })

  it('shows the audio, the three slots, the speeds, the tap and the other controls', async () => {
    const editor = await loadedEditor()

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toContain('<audio')
    expect(html).toContain('src="/api/posts/7/audio?token=first"')
    expect(html.split('lyrics-editor__slot ').length - 1).toBe(3)
    for (const label of ['1×', '0,75×', '0,5×']) expect(html).toContain(label)
    for (const label of [
      'Тап',
      'Скасувати',
      '♪',
      'Програш',
      'Додати рядок у кінець',
      'До наступного невідстуканого',
      'Знайти синхронізацію',
      'Знайти просто текст',
      'Вставити текст',
      'Зберегти',
      'Зберегти й зробити відео',
    ]) {
      expect(html).toContain(label)
    }
    expect(html).toContain('type="range"')
    for (const old of ['Пауза в тексті', 'Рядок нижче', 'Підставити текст']) {
      expect(html).not.toContain(old)
    }
  })

  it('names an inserted pause row «(програш)» in the slot and the field, never «(пауза)»', async () => {
    const editor = await loadedEditor()
    editor.addPause(5000)
    editor.retime(1)

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toContain('lyrics-editor__slot lyrics-editor__slot--current">(програш)<')
    expect(html).toContain('placeholder="(програш)"')
    expect(html).not.toContain('(пауза)')
  })

  it('puts the line to tap next in the current slot, with its neighbours around it', async () => {
    const editor = await loadedEditor()

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toMatch(/lyrics-editor__slot lyrics-editor__slot--side">Другий</)
    expect(html).toMatch(/lyrics-editor__slot lyrics-editor__slot--current">Третій</)
  })

  it('lists the lines with their times and marks the ones inside the fragment', async () => {
    const editor = await loadedEditor()

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toContain('0:01.0')
    expect(html).toContain('0:20.0')
    expect(html.split('у відео</span>')).toHaveLength(2)
    expect(html).toContain('Грати з цього рядка')
    expect(html).toContain('Відстукати звідси')
  })

  it('keeps the save button off until something changed, and the render button on', async () => {
    const editor = await loadedEditor()

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toMatch(/<button[^>]*disabled[^>]*>\s*Зберегти\s*<\/button>/)
    expect(html).not.toMatch(/<button[^>]*disabled[^>]*>\s*Зберегти й зробити відео/)
  })

  it('offers Genius first among the sources of plain text', async () => {
    const editor = await loadedEditor()

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toMatch(/<option value="genius" selected[^>]*>\s*Genius/)
    expect(html).toContain('YouTube Music')
    expect(html).toContain('LRCLIB')
  })

  it('names the other posts of the track that share the text, and only then', async () => {
    const shared = await loadedEditor(makeLyricsView({ other_post_ids: [4, 9] }))
    const alone = await loadedEditor()

    const html = await render(LyricsEditor, { editor: shared, webApp: undefined })
    const plain = await render(LyricsEditor, { editor: alone, webApp: undefined })

    expect(html).toContain('Текст спільний з постами #4, #9')
    expect(html).toContain('їхні наступні рендери візьмуть новий текст')
    expect(plain).not.toContain('Текст спільний')
  })

  it('shows the first broken rule as an alert', async () => {
    const editor = await loadedEditor()
    editor.addLine(3)

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toContain('Рядок 4 порожній і без часу')
    expect(html).toContain('role="alert"')
  })

  it('renders every text as text, never as markup or a style string', async () => {
    const hostile = '<img src=x onerror=alert(1)>'
    const editor = await loadedEditor(
      makeLyricsView({ lines: [{ text: hostile, start_ms: 1000, source: 'lrclib' }] }),
    )
    editor.edit(0, hostile)

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
    expect(html).not.toContain('style=')
    expect(html).not.toContain('<script')
  })

  it('shows the sentence of a refused lookup and the saved sentence', async () => {
    const editor = await loadedEditor()
    editor.setPlaying(true)
    editor.tapAt(500)

    const html = await render(LyricsEditor, { editor, webApp: undefined })

    expect(html).toContain('раніше за попередній рядок')
  })

  it('shows a refused tap right under the tap button, and a save sentence after the list', async () => {
    const editor = await loadedEditor()
    editor.tapAt(500)

    const refused = await render(LyricsEditor, { editor, webApp: undefined })
    const sentence = refused.indexOf('Увімкніть відтворення')
    expect(sentence).toBeGreaterThan(refused.indexOf('lyrics-editor__tap'))
    expect(sentence).toBeLessThan(refused.indexOf('lyrics-editor__tools'))
    expect(refused.split('Увімкніть відтворення')).toHaveLength(2)

    const server = fakeServer({
      'GET /api/posts/7/lyrics': () => jsonResponse(STORED),
      'GET /api/posts/7/fragment': () => jsonResponse(makeFragmentView()),
      'PUT /api/posts/7/lyrics': () => jsonResponse({ ...STORED, lyrics_version: 'v2' }),
    })
    const saving = useLyricsEditor(server.client, 7, { confirm: async () => true })
    await saving.load()
    saving.edit(0, 'Змінений')
    await saving.save()
    const savedHtml = await render(LyricsEditor, { editor: saving, webApp: undefined })
    expect(savedHtml.indexOf('Текст збережено.')).toBeGreaterThan(
      savedHtml.indexOf('lyrics-editor__lines'),
    )
    expect(savedHtml).not.toContain('Увімкніть відтворення')
  })

  it('reports unsaved text to the leave guard, and clean text as nothing to lose', async () => {
    const guard = { setDirty: vi.fn(), leave: vi.fn(), othersDirty: vi.fn() }
    const dirty = await loadedEditor()
    dirty.edit(0, 'Змінений')
    const app = createSSRApp(LyricsEditor, { editor: dirty, webApp: undefined })
    app.provide(LEAVE_GUARD, guard)
    await renderToString(app)
    expect(guard.setDirty).toHaveBeenLastCalledWith('lyrics', true)

    const clean = await loadedEditor()
    const second = createSSRApp(LyricsEditor, { editor: clean, webApp: undefined })
    second.provide(LEAVE_GUARD, guard)
    await renderToString(second)
    expect(guard.setDirty).toHaveBeenLastCalledWith('lyrics', false)
  })

  it('turns the closing confirmation on while the text is unsaved and not before', async () => {
    const webApp = { enableClosingConfirmation: vi.fn(), disableClosingConfirmation: vi.fn() }
    const clean = await loadedEditor()
    await render(LyricsEditor, { editor: clean, webApp })
    expect(webApp.enableClosingConfirmation).not.toHaveBeenCalled()

    const dirty = await loadedEditor()
    dirty.edit(0, 'Змінений')
    await render(LyricsEditor, { editor: dirty, webApp })
    expect(webApp.enableClosingConfirmation).toHaveBeenCalledOnce()
  })

  it('calls the confirmation members only where the client has them', () => {
    const webApp = { enableClosingConfirmation: vi.fn(), disableClosingConfirmation: vi.fn() }

    setClosingConfirmation(webApp as unknown as TelegramWebApp, true)
    setClosingConfirmation(webApp as unknown as TelegramWebApp, false)

    expect(webApp.enableClosingConfirmation).toHaveBeenCalledOnce()
    expect(webApp.disableClosingConfirmation).toHaveBeenCalledOnce()
    expect(() => setClosingConfirmation(undefined, true)).not.toThrow()
    expect(() => setClosingConfirmation({} as TelegramWebApp, true)).not.toThrow()
  })
})

describe('focus after a click', () => {
  function fakeButton() {
    const button = { blur: vi.fn(), closest: vi.fn((): unknown => button) }
    return button
  }

  it('blurs the button a mouse or touch click landed on, also from a child of the button', () => {
    const button = fakeButton()
    const child = { closest: vi.fn(() => button) }

    blurAfterClick({ target: child, detail: 1 })

    expect(child.closest).toHaveBeenCalledWith('button, input[type="range"]')
    expect(button.blur).toHaveBeenCalledOnce()
  })

  it('keeps the focus of a click made with the keyboard, which has no detail', () => {
    const button = fakeButton()

    blurAfterClick({ target: button, detail: 0 })

    expect(button.blur).not.toHaveBeenCalled()
  })

  it('does nothing for a click outside any button or on something that is not an element', () => {
    expect(() => blurAfterClick({ target: { closest: () => null }, detail: 1 })).not.toThrow()
    expect(() => blurAfterClick({ target: null, detail: 1 })).not.toThrow()
    expect(() => blurAfterClick({ target: {}, detail: 1 })).not.toThrow()
  })

  it('looks for a button or a range slider, and blurs the slider after a drag', () => {
    const slider = { blur: vi.fn() }
    const target = {
      closest: vi.fn((selector: string) => (selector.includes('range') ? slider : null)),
    }

    blurAfterClick({ target, detail: 1 })

    expect(target.closest).toHaveBeenCalledWith('button, input[type="range"]')
    expect(slider.blur).toHaveBeenCalledOnce()
  })

  it('keeps the focus of a slider used with the arrow keys', () => {
    const slider = { blur: vi.fn(), closest: vi.fn(() => slider) }

    blurAfterClick({ target: slider, detail: 0 })

    expect(slider.blur).not.toHaveBeenCalled()
  })

  it('leaves no focus on the tap button, which acts on pointerdown', () => {
    const tapButton = fakeButton()

    blurButton(tapButton)

    expect(tapButton.blur).toHaveBeenCalledOnce()
  })
})

describe('releaseEditor', () => {
  function resources(overrides: Record<string, unknown> = {}) {
    const calls: string[] = []
    const audio = {
      pause: vi.fn(() => calls.push('pause')),
      removeAttribute: vi.fn((name: string) => calls.push(`remove ${name}`)),
      load: vi.fn(() => calls.push('load')),
    }
    const webApp = {
      disableClosingConfirmation: vi.fn(() => calls.push('confirmation off')),
    }
    const given = {
      detachKeys: vi.fn(() => calls.push('keys')),
      restoreSwipes: vi.fn(() => calls.push('swipes')),
      audio,
      webApp: webApp as unknown as TelegramWebApp,
      setPlaying: vi.fn((playing: boolean) => calls.push(`playing ${playing}`)),
      ...overrides,
    }
    return { given, calls, audio, webApp }
  }

  it('detaches the keys, restores the swipes, turns the confirmation off and tears the audio down', () => {
    const { given, calls, audio, webApp } = resources()

    releaseEditor(given)

    expect(given.detachKeys).toHaveBeenCalledOnce()
    expect(given.restoreSwipes).toHaveBeenCalledOnce()
    expect(webApp.disableClosingConfirmation).toHaveBeenCalledOnce()
    expect(given.setPlaying).toHaveBeenCalledWith(false)
    expect(audio.pause).toHaveBeenCalledOnce()
    expect(audio.removeAttribute).toHaveBeenCalledWith('src')
    expect(audio.load).toHaveBeenCalledOnce()
    expect(calls.indexOf('pause')).toBeLessThan(calls.indexOf('remove src'))
    expect(calls.indexOf('remove src')).toBeLessThan(calls.indexOf('load'))
  })

  it('still undoes the rest when there is no audio element or no Telegram', () => {
    const { given } = resources({ audio: null, webApp: undefined })

    expect(() => releaseEditor(given)).not.toThrow()

    expect(given.detachKeys).toHaveBeenCalledOnce()
    expect(given.restoreSwipes).toHaveBeenCalledOnce()
    expect(given.setPlaying).toHaveBeenCalledWith(false)
  })
})

describe('LyricsSection', () => {
  const base = { client: fakeServer({}).client, postId: 7, webApp: undefined }

  it('offers to open the editor and loads nothing until then', async () => {
    const html = await render(LyricsSection, base)

    expect(html).toContain('Текст')
    expect(html).toContain('Редагувати текст')
    expect(html).not.toContain('<audio')
  })
})

function keyEvent(
  key: string,
  extra: Record<string, unknown> = {},
  target?: object,
  type = 'keydown',
): Event {
  const event = Object.assign(new Event(type, { cancelable: true }), {
    key,
    repeat: false,
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    ...extra,
  })
  if (target !== undefined) Object.defineProperty(event, 'target', { value: target })
  return event
}

describe('attachKeys', () => {
  function attached() {
    const host = new EventTarget()
    const handlers = { tap: vi.fn(), undo: vi.fn() }
    const detach = attachKeys(host, handlers)
    return { host, handlers, detach }
  }

  it('taps on Space and undoes on Backspace, and stops the page from acting on them', () => {
    const { host, handlers } = attached()
    const space = keyEvent(' ')
    const back = keyEvent('Backspace')

    host.dispatchEvent(space)
    host.dispatchEvent(back)

    expect(handlers.tap).toHaveBeenCalledOnce()
    expect(handlers.undo).toHaveBeenCalledOnce()
    expect(space.defaultPrevented).toBe(true)
    expect(back.defaultPrevented).toBe(true)
  })

  it.each([
    ['a text input', { tagName: 'INPUT', type: 'text' }],
    ['an input without a type', { tagName: 'INPUT' }],
    ['a text area', { tagName: 'TEXTAREA' }],
    ['a select', { tagName: 'SELECT' }],
    ['an editable element', { tagName: 'DIV', isContentEditable: true }],
  ])('leaves the keys to %s', (_name, target) => {
    const { host, handlers } = attached()
    const space = keyEvent(' ', {}, target)
    const back = keyEvent('Backspace', {}, target)

    host.dispatchEvent(space)
    host.dispatchEvent(back)

    expect(handlers.tap).not.toHaveBeenCalled()
    expect(handlers.undo).not.toHaveBeenCalled()
    expect(space.defaultPrevented).toBe(false)
    expect(back.defaultPrevented).toBe(false)
  })

  it('cancels Space on every handled path, a refused tap included, and its release too', async () => {
    const editor = await loadedEditor()
    const host = new EventTarget()
    const refusedTap = vi.fn(() => editor.tapAt(500))
    attachKeys(host, { tap: refusedTap, undo: vi.fn() })
    const down = keyEvent(' ')
    const up = keyEvent(' ', {}, undefined, 'keyup')

    host.dispatchEvent(down)
    host.dispatchEvent(up)

    expect(refusedTap).toHaveReturnedWith(false)
    expect(editor.message.value).toContain('відтворення')
    expect(down.defaultPrevented).toBe(true)
    expect(up.defaultPrevented).toBe(true)
  })

  it('forgets a lost Space release on blur, so the next Space on a button is left alone', () => {
    const { host } = attached()
    host.dispatchEvent(keyEvent(' '))
    host.dispatchEvent(new Event('blur'))

    const up = keyEvent(' ', {}, { tagName: 'BUTTON' }, 'keyup')
    host.dispatchEvent(up)

    expect(up.defaultPrevented).toBe(false)
  })

  it('forgets a lost Space release at any keydown that is not handled', () => {
    const { host } = attached()
    host.dispatchEvent(keyEvent(' '))
    host.dispatchEvent(keyEvent(' ', {}, { tagName: 'BUTTON' }))

    const up = keyEvent(' ', {}, { tagName: 'BUTTON' }, 'keyup')
    host.dispatchEvent(up)

    expect(up.defaultPrevented).toBe(false)
  })

  it('still cancels the release of a handled Space, also after a held-key repeat', () => {
    const { host } = attached()
    host.dispatchEvent(keyEvent(' '))
    host.dispatchEvent(keyEvent(' ', { repeat: true }))

    const up = keyEvent(' ', {}, undefined, 'keyup')
    host.dispatchEvent(up)

    expect(up.defaultPrevented).toBe(true)
  })

  it('leaves a Space on a button, and its release, to the button', () => {
    const { host } = attached()
    const down = keyEvent(' ', {}, { tagName: 'BUTTON' })
    const up = keyEvent(' ', {}, { tagName: 'BUTTON' }, 'keyup')

    host.dispatchEvent(down)
    host.dispatchEvent(up)

    expect(down.defaultPrevented).toBe(false)
    expect(up.defaultPrevented).toBe(false)
  })

  it('leaves the keys to a button and to the slider', () => {
    const { host, handlers } = attached()
    const onButton = keyEvent(' ', {}, { tagName: 'BUTTON' })
    const onSlider = keyEvent('Backspace', {}, { tagName: 'INPUT', type: 'range' })

    host.dispatchEvent(onButton)
    host.dispatchEvent(onSlider)

    expect(handlers.tap).not.toHaveBeenCalled()
    expect(handlers.undo).not.toHaveBeenCalled()
    expect(onButton.defaultPrevented).toBe(false)
    expect(onSlider.defaultPrevented).toBe(false)
  })

  it('still taps on the page body and on a plain element', () => {
    const { host, handlers } = attached()

    host.dispatchEvent(keyEvent(' ', {}, { tagName: 'BODY' }))
    host.dispatchEvent(keyEvent(' ', {}, { tagName: 'DIV' }))

    expect(handlers.tap).toHaveBeenCalledTimes(2)
  })

  it('knows a control target', () => {
    expect(isControlTarget({ tagName: 'BUTTON' })).toBe(true)
    expect(isControlTarget({ tagName: 'INPUT', type: 'range' })).toBe(true)
    expect(isControlTarget({ tagName: 'INPUT', type: 'text' })).toBe(false)
    expect(isControlTarget(null)).toBe(false)
  })

  it('ignores a held key, other keys and keys with a modifier', () => {
    const { host, handlers } = attached()

    host.dispatchEvent(keyEvent(' ', { repeat: true }))
    host.dispatchEvent(keyEvent('Backspace', { repeat: true }))
    host.dispatchEvent(keyEvent('a'))
    host.dispatchEvent(keyEvent(' ', { ctrlKey: true }))
    host.dispatchEvent(keyEvent('Backspace', { metaKey: true }))
    host.dispatchEvent(keyEvent(' ', { altKey: true }))

    expect(handlers.tap).not.toHaveBeenCalled()
    expect(handlers.undo).not.toHaveBeenCalled()
  })

  it('removes its listeners when detached', () => {
    const { host, handlers, detach } = attached()

    detach()
    host.dispatchEvent(keyEvent(' '))
    host.dispatchEvent(keyEvent('Backspace'))

    expect(handlers.tap).not.toHaveBeenCalled()
    expect(handlers.undo).not.toHaveBeenCalled()
  })

  it('knows a text target from anything else', () => {
    expect(isTextTarget(null)).toBe(false)
    expect(isTextTarget('x')).toBe(false)
    expect(isTextTarget({ tagName: 'INPUT', type: 'checkbox' })).toBe(false)
    expect(isTextTarget({ tagName: 'INPUT', type: 'search' })).toBe(true)
  })
})
