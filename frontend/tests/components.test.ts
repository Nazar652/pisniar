import { createSSRApp, defineComponent, h, ref } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { describe, expect, it } from 'vitest'
import ArtistsEditor from '../src/components/ArtistsEditor.vue'
import CaptionPreview from '../src/components/CaptionPreview.vue'
import FieldBlock from '../src/components/FieldBlock.vue'
import GenresEditor from '../src/components/GenresEditor.vue'
import LinksEditor from '../src/components/LinksEditor.vue'
import ListEditor from '../src/components/ListEditor.vue'
import NoticeBanner from '../src/components/NoticeBanner.vue'
import PostCard from '../src/components/PostCard.vue'
import PostEditor from '../src/views/PostEditor.vue'
import PostList from '../src/views/PostList.vue'
import { formFromDetail } from '../src/editor/form'
import { fakeServer, makeCaption, makeDetail, makeMeta } from './support'

const noError = (): undefined => undefined

async function render(component: object, props: Record<string, unknown>): Promise<string> {
  return renderToString(createSSRApp(component, props))
}

// editors use `defineModel`, so they are rendered under a parent that owns the value
async function renderModel(component: object, model: unknown, props: Record<string, unknown>) {
  const Parent = defineComponent({
    setup() {
      const value = ref(model)
      return () => h(component, { modelValue: value.value, ...props })
    },
  })
  return renderToString(createSSRApp(Parent))
}

const summary = {
  id: 7,
  status_label: 'Чернетка готова',
  title: 'Тема',
  artists: ['Океан Ельзи', 'Друга Ріка'],
  created_at: '2026-10-01T10:00:00Z',
  published_at: null,
  editable: true,
  not_editable_reason: null,
  has_video: false,
}

describe('PostCard', () => {
  it('shows the title, the artists, the status and the id', async () => {
    const html = await render(PostCard, { post: summary })

    expect(html).toContain('Тема')
    expect(html).toContain('Океан Ельзи, Друга Ріка')
    expect(html).toContain('Чернетка готова')
    expect(html).toContain('#7')
  })

  it('marks a post with a video and explains why a post is locked', async () => {
    const html = await render(PostCard, {
      post: {
        ...summary,
        has_video: true,
        editable: false,
        not_editable_reason: 'Пост зараз обробляється.',
      },
    })

    expect(html).toContain('🎬')
    expect(html).toContain('post-card--locked')
    expect(html).toContain('Пост зараз обробляється.')
  })

  it('does not print a reason for a post that can be edited', async () => {
    const html = await render(PostCard, { post: { ...summary, not_editable_reason: 'Ні' } })

    expect(html).not.toContain('post-card__reason')
  })

  it('names a post without a title', async () => {
    expect(await render(PostCard, { post: { ...summary, title: null } })).toContain('Без назви')
  })
})

describe('PostList', () => {
  it('starts with a loading line, not an empty list', async () => {
    const server = fakeServer({})

    const html = await render(PostList, { client: server.client })

    expect(html).toContain('Завантаження')
    expect(html).not.toContain('Постів поки немає')
  })
})

describe('FieldBlock', () => {
  it('shows the label, the hint and the error', async () => {
    const html = await render(FieldBlock, { label: 'Рік', hint: 'Підказка', error: 'Помилка' })

    expect(html).toContain('Рік')
    expect(html).toContain('Підказка')
    expect(html).toContain('Помилка')
    expect(html).not.toContain('Повернути автоматичне')
  })

  it('offers «back to automatic» for a manual field and flips the wording once marked', async () => {
    const plain = await render(FieldBlock, { label: 'Рік', manual: true })
    const marked = await render(FieldBlock, { label: 'Рік', manual: true, reverting: true })

    expect(plain).toContain('Повернути автоматичне')
    expect(plain).toContain('змінено вручну')
    expect(marked).toContain('Лишити ручним')
  })
})

describe('NoticeBanner', () => {
  it('renders nothing without a notice', async () => {
    expect(await render(NoticeBanner, { notice: null })).not.toContain('notice')
  })

  it('announces an error as an alert and a success as a status', async () => {
    const error = await render(NoticeBanner, { notice: { kind: 'error', text: 'Погано' } })
    const success = await render(NoticeBanner, { notice: { kind: 'success', text: 'Добре' } })

    expect(error).toContain('role="alert"')
    expect(success).toContain('role="status"')
    expect(success).toContain('notice--success')
  })
})

describe('CaptionPreview', () => {
  it('shows text, a safe link opening in a new tab, a break and the counter', async () => {
    const view = makeCaption({
      segments: [
        { kind: 'text', text: 'Тема', href: null },
        { kind: 'line_break', text: null, href: null },
        { kind: 'link', text: 'Spotify', href: 'https://open.spotify.com/x' },
      ],
      visible_length: 120,
    })

    const html = await render(CaptionPreview, { view, message: '' })

    expect(html).toContain('href="https://open.spotify.com/x"')
    expect(html).toContain('rel="noopener noreferrer"')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('<br>')
    expect(html).toContain('120/1024')
  })

  it('escapes markup in the text instead of rendering it', async () => {
    const view = makeCaption({
      segments: [{ kind: 'text', text: '<img src=x onerror=alert(1)>', href: null }],
    })

    const html = await render(CaptionPreview, { view, message: '' })

    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })

  it('never makes an anchor of a script link', async () => {
    const view = makeCaption({
      segments: [{ kind: 'link', text: 'x', href: 'javascript:alert(1)' }],
    })

    expect(await render(CaptionPreview, { view, message: '' })).not.toContain('<a ')
  })

  it('marks an over-limit caption and lists what did not fit', async () => {
    const view = makeCaption({
      over_limit: true,
      visible_length: 1100,
      dropped_genres: ['Рок'],
    })

    const html = await render(CaptionPreview, { view, message: '' })

    expect(html).toContain('preview__count--over')
    expect(html).toContain('Жанри не влізли: Рок.')
  })

  it('shows a placeholder before the first preview and the server sentence on failure', async () => {
    expect(await render(CaptionPreview, { view: null, message: '' })).toContain('з’явиться')
    expect(await render(CaptionPreview, { view: null, message: 'Збій' })).toContain('Збій')
  })
})

describe('ArtistsEditor', () => {
  const rows = formFromDetail(makeDetail()).artists

  it('shows each artist with the default hashtag as the placeholder', async () => {
    const html = await renderModel(ArtistsEditor, rows, { disabled: false, errorOf: noError })

    expect(html).toContain('value="Океан Ельзи"')
    expect(html).toContain('placeholder="#ОкеанЕльзи"')
    expect(html).toContain('value="DrugaRika"')
  })

  it('labels the name and the hashtag and explains the post format', async () => {
    const html = await renderModel(ArtistsEditor, rows, { disabled: false, errorOf: noError })

    expect(html).toContain('Перший рядок поста: «Ім’я 1, Ім’я 2 — Назва треку»')
    expect(html.match(/Ім’я — у першому рядку поста/g)).toHaveLength(2)
    expect(html.match(/artist__label">Хештег</g)).toHaveLength(2)
    expect(html).toContain('порожньо — буде #ОкеанЕльзи')
  })

  it('shows no default-hashtag hint for an artist without one', async () => {
    const bare = [{ name: 'Х', hashtag: '', defaultHashtag: null }]

    const html = await renderModel(ArtistsEditor, bare, { disabled: false, errorOf: noError })

    expect(html).not.toContain('порожньо — буде')
  })

  it('shows the error of a row next to it', async () => {
    const html = await renderModel(ArtistsEditor, rows, {
      disabled: false,
      errorOf: (field: string) =>
        field === 'artists[1].hashtag' ? 'Хештег недійсний.' : undefined,
    })

    expect(html).toContain('Хештег недійсний.')
  })

  it('disables the controls while locked and keeps the last artist from being removed', async () => {
    const locked = await renderModel(ArtistsEditor, rows, { disabled: true, errorOf: noError })
    const single = await renderModel(ArtistsEditor, rows.slice(0, 1), {
      disabled: false,
      errorOf: noError,
    })

    expect(locked).toContain('disabled')
    expect(single).toMatch(/btn--danger[^>]*disabled/)
  })
})

describe('ListEditor', () => {
  it('lists the items and offers the suggestions', async () => {
    const html = await renderModel(ListEditor, ['українська'], {
      disabled: false,
      suggestions: ['англійська'],
      listId: 'languages',
      placeholder: 'Додати мову',
      fieldName: 'languages',
      errorOf: (field: string) => (field === 'languages[0]' ? 'Двічі.' : undefined),
    })

    expect(html).toContain('value="українська"')
    expect(html).toContain('<option value="англійська">')
    expect(html).toContain('Двічі.')
  })
})

describe('GenresEditor', () => {
  it('shows the genre with its default hashtag and the dictionary suggestions', async () => {
    const rows = formFromDetail(makeDetail()).genres

    const html = await renderModel(GenresEditor, rows, {
      disabled: false,
      meta: makeMeta(),
      errorOf: noError,
    })

    expect(html).toContain('value="Рок"')
    expect(html).toContain('placeholder="#Рок"')
    expect(html).toContain('<option value="Рок">')
  })
})

describe('LinksEditor', () => {
  it('shows the links by platform name and offers the platforms without a row', async () => {
    const rows = formFromDetail(makeDetail()).links

    const html = await renderModel(LinksEditor, rows, {
      disabled: false,
      meta: makeMeta(),
      removed: ['deezer'],
      errorOf: noError,
    })

    expect(html).toContain('Spotify')
    expect(html).toContain('YouTube Music (аудіо для відео)')
    expect(html).toContain('Deezer (прибрано)')
    expect(html).not.toContain('tidal')
    expect(html).toContain('YouTube (кліп)')
  })

  it('has no picker once every platform has a row', async () => {
    const meta = makeMeta()
    const rows = meta.platforms.map((platform) => ({
      platform: platform.key,
      url: 'https://x.test',
    }))

    const html = await renderModel(LinksEditor, rows, {
      disabled: false,
      meta,
      removed: [],
      errorOf: noError,
    })

    expect(html).not.toContain('<select')
  })
})

describe('PostEditor', () => {
  it('starts with a loading line and touches neither the network nor the Telegram button', async () => {
    const server = fakeServer({})

    const html = await render(PostEditor, { client: server.client, postId: 7, webApp: undefined })

    expect(html).toContain('Завантаження')
    expect(server.requests).toHaveLength(0)
  })
})
