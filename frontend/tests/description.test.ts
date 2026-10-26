import { describe, expect, it } from 'vitest'
import {
  confirmWithHosts,
  descriptionBudgetNote,
  descriptionCounter,
  descriptionHelp,
  descriptionLength,
  destinationsNote,
  destinationWarnings,
  wrapAsLink,
} from '../src/editor/description'
import { makeCaption } from './support'

describe('descriptionLength', () => {
  it('counts code points, so an emoji is one and a line break is one', () => {
    expect(descriptionLength('ab')).toBe(2)
    expect(descriptionLength('a😀')).toBe(2)
    expect(descriptionLength('a\r\nb')).toBe(3)
    expect(descriptionCounter('привіт', 500)).toBe('6/500')
  })
})

describe('wrapAsLink', () => {
  it('turns the selection into the link text and selects the address', () => {
    const edit = wrapAsLink('Слухайте тут зараз', 9, 12)

    expect(edit.text).toBe('Слухайте [тут](https://) зараз')
    expect(edit.text.slice(edit.selectionStart, edit.selectionEnd)).toBe('https://')
  })

  it('inserts a template at the cursor and selects its text', () => {
    const edit = wrapAsLink('Привіт, !', 8, 8)

    expect(edit.text).toBe('Привіт, [текст](https://)!')
    expect(edit.text.slice(edit.selectionStart, edit.selectionEnd)).toBe('текст')
  })

  it('works on an empty field and with a backwards selection', () => {
    expect(wrapAsLink('', 0, 0).text).toBe('[текст](https://)')
    expect(wrapAsLink('abc', 2, 1).text).toBe('a[b](https://)c')
  })
})

describe('descriptionBudgetNote', () => {
  it('says nothing without a description or a preview', () => {
    expect(descriptionBudgetNote(null, 'Опис', false)).toBeNull()
    expect(descriptionBudgetNote(makeCaption({ over_limit: true }), ' ', true)).toBeNull()
  })

  it('names what the caption gave up to make room', () => {
    const view = makeCaption({ dropped_genres: ['рок'], release_title_dropped: true })

    expect(descriptionBudgetNote(view, 'Опис', false)).toBe(
      'Щоб опис помістився, з підпису прибрано: жанри, назву релізу.',
    )
    expect(descriptionBudgetNote(makeCaption(), 'Опис', false)).toBeNull()
  })

  it('gives the number to cut when the server blames the description', () => {
    const view = makeCaption({ over_limit: true, visible_length: 1100, limit: 1024 })

    expect(descriptionBudgetNote(view, 'Опис', true)).toContain('на 76 символів')
  })

  it('does not blame the description for a caption that is long without it', () => {
    const view = makeCaption({ over_limit: true, visible_length: 1100, limit: 1024 })

    expect(descriptionBudgetNote(view, 'Опис', false)).toBeNull()
  })
})

describe('link destinations', () => {
  const links = [
    { label: 'Spotify', host: 'open.spotify.com', warnings: [] },
    { label: 'ще раз', host: 'open.spotify.com', warnings: [] },
    { label: 'банк', host: 'xn--80ak6aa92e.com', warnings: ['адреса містить нелатинські літери'] },
  ]

  it('names each real host once, in order', () => {
    expect(destinationsNote(links)).toBe(
      'Посилання ведуть на: open.spotify.com, xn--80ak6aa92e.com.',
    )
    expect(destinationsNote([])).toBeNull()
  })

  it('turns the server warnings into sentences naming the link and its host', () => {
    expect(destinationWarnings(links)).toEqual([
      '«банк» (xn--80ak6aa92e.com): адреса містить нелатинські літери.',
    ])
  })

  it('adds the hosts to a confirmation only when there are links', () => {
    expect(confirmWithHosts('Опублікувати?', [])).toBe('Опублікувати?')
    expect(confirmWithHosts('Опублікувати?', links.slice(0, 1))).toBe(
      'Опублікувати? Посилання ведуть на: open.spotify.com.',
    )
  })

  it('shows the link limit from the server in the help line', () => {
    expect(descriptionHelp(10)).toContain('не більше 10')
  })
})
