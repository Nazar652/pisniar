import type { Schemas } from '../api/outcome'

export type PostDetail = Schemas['PostDetail']
export type FieldsInput = Schemas['FieldsInput']
export type FieldsUpdate = Schemas['FieldsUpdate']
export type MetaResponse = Schemas['MetaResponse']

export interface ArtistRow {
  name: string
  hashtag: string
  defaultHashtag: string | null
}

export interface GenreRow {
  name: string
  hashtag: string
  defaultHashtag: string | null
}

export interface LinkRow {
  platform: string
  url: string
}

export interface FormState {
  title: string
  artists: ArtistRow[]
  languages: string[]
  genres: GenreRow[]
  year: string
  releaseType: string
  releaseTitle: string
  albumLink: string
  description: string
  links: LinkRow[]
}

export const FIELD_LABELS: Record<string, string> = {
  title: 'Назва',
  artists: 'Артисти',
  year: 'Рік',
  release_type: 'Тип релізу',
  release_title: 'Назва релізу',
  album_id_spotify: 'Лінк на альбом',
}

export const YEAR_PROBLEM = 'Рік — чотири цифри або порожньо.'

export function formFromDetail(detail: PostDetail): FormState {
  return {
    title: detail.title ?? '',
    artists: detail.artists.map((artist) => ({
      name: artist.name,
      hashtag: artist.hashtag ?? '',
      defaultHashtag: artist.default_hashtag,
    })),
    languages: [...detail.languages],
    genres: detail.genres.map((genre) => ({
      name: genre.name,
      hashtag: genre.hashtag ?? '',
      defaultHashtag: genre.default_hashtag,
    })),
    year: detail.year === null ? '' : String(detail.year),
    releaseType: detail.release_type ?? '',
    releaseTitle: detail.release_title ?? '',
    albumLink: detail.album_url ?? '',
    description: detail.description ?? '',
    // a link the caption never shows stays stored: it has no row, and the server keeps what is absent
    links: Object.entries(detail.platform_links)
      .filter(([, link]) => link.shown_in_caption)
      .map(([platform, link]) => ({ platform, url: link.url })),
  }
}

export function yearProblem(year: string): string | null {
  const trimmed = year.trim()
  return trimmed === '' || /^\d{4}$/.test(trimmed) ? null : YEAR_PROBLEM
}

function orNull(value: string): string | null {
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

// the year is sent as a number only when it reads as one; the server checks the range
export function toFieldsInput(form: FormState, revert: readonly string[]): FieldsInput {
  const year = form.year.trim()
  return {
    title: form.title,
    artists: form.artists.map((artist) => ({ name: artist.name, hashtag: orNull(artist.hashtag) })),
    languages: [...form.languages],
    genres: form.genres.map((genre) => ({ name: genre.name, hashtag: orNull(genre.hashtag) })),
    year: year === '' || yearProblem(year) !== null ? null : Number(year),
    release_type: orNull(form.releaseType),
    release_title: orNull(form.releaseTitle),
    album_link: orNull(form.albumLink),
    // the server normalizes the text; only an empty one is sent as none
    description: form.description.trim() === '' ? null : form.description,
    platform_links: Object.fromEntries(
      form.links.filter((row) => row.url.trim() !== '').map((row) => [row.platform, row.url]),
    ),
    revert_to_automatic: [...revert],
  }
}

export function toUpdate(
  form: FormState,
  revert: readonly string[],
  detail: PostDetail,
): FieldsUpdate {
  return {
    ...toFieldsInput(form, revert),
    expected_post_updated_at: detail.expected_post_updated_at,
    expected_track_updated_at: detail.expected_track_updated_at,
  }
}

// the manual-field names whose value in the form differs from the stored one
export function changedManualFields(form: FormState, detail: PostDetail): string[] {
  const stored = formFromDetail(detail)
  const names = (rows: readonly ArtistRow[]) => rows.map((row) => row.name.trim())
  const checks: [string, boolean][] = [
    ['title', form.title.trim() !== stored.title],
    ['artists', JSON.stringify(names(form.artists)) !== JSON.stringify(names(stored.artists))],
    ['year', form.year.trim() !== stored.year],
    ['release_type', form.releaseType.trim() !== stored.releaseType],
    ['release_title', form.releaseTitle.trim() !== stored.releaseTitle],
    ['album_id_spotify', form.albumLink.trim() !== stored.albumLink],
  ]
  return checks.filter(([, changed]) => changed).map(([name]) => name)
}

// a reverted field that was edited again is no longer a revert: the edit wins
export function effectiveRevert(
  revert: readonly string[],
  form: FormState,
  detail: PostDetail,
): string[] {
  const changed = new Set(changedManualFields(form, detail))
  return revert.filter((name) => detail.manual_fields.includes(name) && !changed.has(name))
}

export function isDirty(form: FormState, detail: PostDetail): boolean {
  const current = JSON.stringify(toFieldsInput(form, []))
  return current !== JSON.stringify(toFieldsInput(formFromDetail(detail), []))
}

export function moveItem<T>(items: readonly T[], index: number, delta: number): T[] {
  const target = index + delta
  const moved = items[index]
  if (moved === undefined || target < 0 || target >= items.length) return [...items]
  const result = [...items]
  result.splice(index, 1)
  result.splice(target, 0, moved)
  return result
}

export function removeAt<T>(items: readonly T[], index: number): T[] {
  return items.filter((_, position) => position !== index)
}

export function releaseTypeOptions(
  meta: MetaResponse,
  current: string,
): { value: string; label: string }[] {
  const known = meta.release_types.map((option) => ({ value: option.value, label: option.label }))
  const unknown = current !== '' && !known.some((option) => option.value === current)
  return [
    { value: '', label: 'Не вказано' },
    ...known,
    ...(unknown ? [{ value: current, label: `${current} (з джерела)` }] : []),
  ]
}

export function platformLabel(meta: MetaResponse | null, key: string): string {
  if (key === 'youtube') return 'YouTube (кліп)'
  if (key === 'youtubeMusic') return 'YouTube Music (аудіо для відео)'
  return meta?.platforms.find((platform) => platform.key === key)?.display_name ?? key
}

// platforms that have no row yet, the removed ones first so a removed link is easy to bring back
export function platformsToAdd(
  meta: MetaResponse,
  presentPlatforms: readonly string[],
  removed: readonly string[],
): string[] {
  const present = new Set(presentPlatforms)
  const free = meta.platforms.map((platform) => platform.key).filter((key) => !present.has(key))
  return [
    ...free.filter((key) => removed.includes(key)),
    ...free.filter((key) => !removed.includes(key)),
  ]
}

export function genreDefaultHashtag(meta: MetaResponse | null, name: string): string | null {
  return meta?.genres.find((genre) => genre.name === name.trim())?.default_hashtag ?? null
}

// the warning for a changed YouTube Music link when a video was already rendered from the old one
export function youtubeMusicWarning(form: FormState, detail: PostDetail): string | null {
  if (!detail.has_video) return null
  const stored = detail.platform_links['youtubeMusic']?.url ?? ''
  const current = form.links.find((row) => row.platform === 'youtubeMusic')?.url.trim() ?? ''
  return current === stored
    ? null
    : 'У цього поста вже є відео зі старого аудіо. Воно не перерахується: нове посилання діє лише для наступних відео.'
}
