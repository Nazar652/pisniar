import { describe, expect, it } from 'vitest'
import {
  changedManualFields,
  effectiveRevert,
  formFromDetail,
  genreDefaultHashtag,
  isDirty,
  moveItem,
  platformLabel,
  platformsToAdd,
  releaseTypeOptions,
  removeAt,
  toFieldsInput,
  toUpdate,
  yearProblem,
  YEAR_PROBLEM,
  youtubeMusicWarning,
} from '../src/editor/form'
import { makeDetail, makeMeta } from './support'

describe('formFromDetail and toFieldsInput', () => {
  it('turns the stored values into text fields and back without loss', () => {
    const detail = makeDetail()

    const input = toFieldsInput(formFromDetail(detail), [])

    expect(input.title).toBe('Тема')
    expect(input.artists).toEqual([
      { name: 'Океан Ельзи', hashtag: null },
      { name: 'Друга Ріка', hashtag: 'DrugaRika' },
    ])
    expect(input.year).toBe(2019)
    expect(input.album_link).toBe('https://open.spotify.com/album/4yYXqedvSKuJlgI2IWsB0j')
    expect(input.platform_links).toEqual({
      spotify: 'https://open.spotify.com/track/abc',
      youtubeMusic: 'https://music.youtube.com/watch?v=n4bPzdIs_nc',
    })
  })

  it('gives a link the caption never shows no row and does not send it', () => {
    const detail = makeDetail()

    const form = formFromDetail(detail)

    expect(detail.platform_links).toHaveProperty('tidal')
    expect(form.links.map((row) => row.platform)).toEqual(['spotify', 'youtubeMusic'])
    expect(toFieldsInput(form, []).platform_links).not.toHaveProperty('tidal')
    expect(isDirty(form, detail)).toBe(false)
  })

  it('sends empty text as null and drops link rows without an address', () => {
    const form = formFromDetail(makeDetail())
    form.year = ' '
    form.releaseTitle = ''
    form.albumLink = '  '
    form.links.push({ platform: 'deezer', url: '' })

    const input = toFieldsInput(form, ['year'])

    expect([input.year, input.release_title, input.album_link]).toEqual([null, null, null])
    expect(input.platform_links).not.toHaveProperty('deezer')
    expect(input.revert_to_automatic).toEqual(['year'])
  })

  it('does not send a year that is not four digits', () => {
    const form = formFromDetail(makeDetail())
    form.year = '20x9'

    expect(toFieldsInput(form, []).year).toBeNull()
  })

  it('shows an empty form for a track without values', () => {
    const form = formFromDetail(
      makeDetail({ title: null, year: null, release_type: null, album_url: null }),
    )

    expect([form.title, form.year, form.releaseType, form.albumLink]).toEqual(['', '', '', ''])
  })

  it('adds the versions the save is checked against', () => {
    const update = toUpdate(formFromDetail(makeDetail()), [], makeDetail())

    expect(update.expected_post_updated_at).toBe('2026-10-02T12:00:00Z')
    expect(update.expected_track_updated_at).toBe('2026-10-02T12:00:00Z')
  })
})

describe('yearProblem', () => {
  it.each(['', ' ', '2019', ' 1999 '])('accepts %j', (year) => {
    expect(yearProblem(year)).toBeNull()
  })

  it.each(['abc', '20', '20190', '-2019', '2019.5'])('refuses %j', (year) => {
    expect(yearProblem(year)).toBe(YEAR_PROBLEM)
  })
})

describe('changedManualFields', () => {
  it('lists nothing for an untouched form', () => {
    const detail = makeDetail()

    expect(changedManualFields(formFromDetail(detail), detail)).toEqual([])
    expect(isDirty(formFromDetail(detail), detail)).toBe(false)
  })

  it('lists exactly the manual fields that differ', () => {
    const detail = makeDetail()
    const form = formFromDetail(detail)
    form.title = 'Нова'
    form.releaseTitle = 'Інший'
    form.albumLink = ''

    expect(changedManualFields(form, detail)).toEqual([
      'title',
      'release_title',
      'album_id_spotify',
    ])
  })

  it('counts the artists as changed by names or order and not by a hashtag alone', () => {
    const detail = makeDetail()
    const hashtagOnly = formFromDetail(detail)
    const first = hashtagOnly.artists[0]
    if (first) first.hashtag = 'OE'
    const reordered = formFromDetail(detail)
    reordered.artists = moveItem(reordered.artists, 0, 1)

    expect(changedManualFields(hashtagOnly, detail)).toEqual([])
    expect(changedManualFields(reordered, detail)).toEqual(['artists'])
    expect(isDirty(hashtagOnly, detail)).toBe(true)
  })
})

describe('effectiveRevert', () => {
  it('keeps a marked manual field that was not edited again', () => {
    const detail = makeDetail({ manual_fields: ['year', 'title'] })

    expect(effectiveRevert(['year'], formFromDetail(detail), detail)).toEqual(['year'])
  })

  it('drops a name that is not manual and a field edited after the mark', () => {
    const detail = makeDetail({ manual_fields: ['year'] })
    const form = formFromDetail(detail)
    form.year = '2000'

    expect(effectiveRevert(['year', 'title'], form, detail)).toEqual([])
  })
})

describe('list helpers', () => {
  it('moves an item one place and keeps the others in order', () => {
    expect(moveItem(['a', 'b', 'c'], 0, 1)).toEqual(['b', 'a', 'c'])
    expect(moveItem(['a', 'b', 'c'], 2, -1)).toEqual(['a', 'c', 'b'])
  })

  it('does not move past either end or for an unknown index', () => {
    expect(moveItem(['a', 'b'], 0, -1)).toEqual(['a', 'b'])
    expect(moveItem(['a', 'b'], 1, 1)).toEqual(['a', 'b'])
    expect(moveItem(['a', 'b'], 5, 1)).toEqual(['a', 'b'])
  })

  it('removes one position and leaves the input alone', () => {
    const items = ['a', 'b', 'c']

    expect(removeAt(items, 1)).toEqual(['a', 'c'])
    expect(items).toEqual(['a', 'b', 'c'])
  })
})

describe('releaseTypeOptions', () => {
  it('offers the known types and an empty choice', () => {
    expect(releaseTypeOptions(makeMeta(), 'album').map((option) => option.value)).toEqual([
      '',
      'single',
      'album',
      'ep',
    ])
  })

  it('keeps an unknown value from the source as a labelled choice', () => {
    const options = releaseTypeOptions(makeMeta(), 'mixtape')

    expect(options[options.length - 1]).toEqual({ value: 'mixtape', label: 'mixtape (з джерела)' })
  })
})

describe('platform helpers', () => {
  it('labels the two YouTube keys by what they are and the rest by name', () => {
    const meta = makeMeta()

    expect(platformLabel(meta, 'youtube')).toBe('YouTube (кліп)')
    expect(platformLabel(meta, 'youtubeMusic')).toContain('YouTube Music')
    expect(platformLabel(meta, 'deezer')).toBe('Deezer')
    expect(platformLabel(null, 'deezer')).toBe('deezer')
  })

  it('lists the platforms without a row, removed ones first', () => {
    const meta = makeMeta()

    expect(platformsToAdd(meta, ['spotify'], ['youtube'])).toEqual([
      'youtube',
      'deezer',
      'youtubeMusic',
    ])
    expect(platformsToAdd(meta, ['spotify', 'deezer', 'youtube', 'youtubeMusic'], [])).toEqual([])
  })

  it('finds the default hashtag of a known genre only', () => {
    expect(genreDefaultHashtag(makeMeta(), ' Рок ')).toBe('Рок')
    expect(genreDefaultHashtag(makeMeta(), 'Невідомий')).toBeNull()
    expect(genreDefaultHashtag(null, 'Рок')).toBeNull()
  })
})

describe('youtubeMusicWarning', () => {
  const changed = (url: string) => {
    const detail = makeDetail({ has_video: true })
    const form = formFromDetail(detail)
    const row = form.links.find((link) => link.platform === 'youtubeMusic')
    if (row) row.url = url
    return youtubeMusicWarning(form, detail)
  }

  it('warns for a different address and for a removed one', () => {
    expect(changed('https://music.youtube.com/watch?v=dQw4w9WgXcQ')).not.toBeNull()
    expect(changed('')).not.toBeNull()
  })

  it('stays silent for the same address, with surrounding spaces too', () => {
    expect(changed('https://music.youtube.com/watch?v=n4bPzdIs_nc')).toBeNull()
  })
})
