import { vi } from 'vitest'
import { createApiClient } from '../src/api/client'
import type { Schemas } from '../src/api/outcome'
import type { PostDetail } from '../src/editor/form'
import type { MetaResponse } from '../src/editor/form'

export type Handler = (request: Request) => Response | Promise<Response>

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

export function errorResponse(status: number, code: string, message: string, extra = {}): Response {
  return jsonResponse({ code, message, ...extra }, status)
}

export interface FakeServer {
  client: ReturnType<typeof createApiClient>
  requests: Request[]
  bodies: unknown[]
  handlers: Record<string, Handler>
  callsTo: (key: string) => number
}

// routes are `METHOD /path`; an unknown route answers 404, and every body is recorded as JSON
export function fakeServer(handlers: Record<string, Handler>): FakeServer {
  const requests: Request[] = []
  const bodies: unknown[] = []
  const serve = vi.fn(async (input: Request) => {
    requests.push(input)
    const text = await input.clone().text()
    bodies.push(text === '' ? null : (JSON.parse(text) as unknown))
    const handler = handlers[`${input.method} ${new URL(input.url).pathname}`]
    return handler === undefined ? jsonResponse({}, 404) : handler(input)
  })
  const client = createApiClient({
    baseUrl: 'https://app.test',
    getToken: () => 'token',
    fetch: serve as unknown as typeof fetch,
  })
  const callsTo = (key: string): number =>
    requests.filter((request) => `${request.method} ${new URL(request.url).pathname}` === key)
      .length
  return { client, requests, bodies, handlers, callsTo }
}

export const STAMP = '2026-10-02T12:00:00Z'

export type CaptionView = Schemas['CaptionView']

export function makeCaption(overrides: Partial<CaptionView> = {}): CaptionView {
  return {
    segments: [{ kind: 'text', text: 'Тема', href: null }],
    visible_length: 4,
    limit: 1024,
    dropped_genres: [],
    dropped_platforms: [],
    release_title_dropped: false,
    over_limit: false,
    warnings: [],
    sha256: 'a'.repeat(64),
    ...overrides,
  }
}

export function makeDetail(overrides: Partial<PostDetail> = {}): PostDetail {
  return {
    id: 7,
    has_video: false,
    status_label: 'Чернетка готова',
    editable: true,
    not_editable_reason: null,
    fragment: {
      start_ms: null,
      end_ms: null,
      choosable: false,
      preparable: false,
      preparing: false,
      reason: 'Фрагмент ще не обрано.',
    },
    created_at: STAMP,
    published_at: null,
    title: 'Тема',
    artists: [
      { name: 'Океан Ельзи', hashtag: null, default_hashtag: 'ОкеанЕльзи', spotify_id: 'a1' },
      { name: 'Друга Ріка', hashtag: 'DrugaRika', default_hashtag: 'ДругаРіка', spotify_id: null },
    ],
    languages: ['українська'],
    genres: [{ name: 'Рок', hashtag: null, default_hashtag: 'Рок', source: 'deezer' }],
    year: 2019,
    release_type: 'album',
    release_title: 'Альбом',
    album_id_spotify: '4yYXqedvSKuJlgI2IWsB0j',
    album_url: 'https://open.spotify.com/album/4yYXqedvSKuJlgI2IWsB0j',
    platform_links: {
      spotify: {
        url: 'https://open.spotify.com/track/abc',
        source: 'spotify',
        match: 'exact_isrc',
      },
      youtubeMusic: {
        url: 'https://music.youtube.com/watch?v=n4bPzdIs_nc',
        source: 'ytmusic',
        match: 'confirmed',
      },
    },
    manual_fields: ['year'],
    removed_platforms: ['tidal'],
    caption: makeCaption(),
    stored_caption_html: '<b>x</b>',
    stored_caption_matches: true,
    other_post_ids: [],
    expected_post_updated_at: STAMP,
    expected_track_updated_at: STAMP,
    ...overrides,
  }
}

export function makeMeta(): MetaResponse {
  return {
    platforms: [
      {
        key: 'spotify',
        display_name: 'Spotify',
        hosts: [],
        host_suffixes: [],
        requires_track_link: true,
      },
      {
        key: 'tidal',
        display_name: 'Tidal',
        hosts: [],
        host_suffixes: [],
        requires_track_link: true,
      },
      {
        key: 'youtube',
        display_name: 'YouTube',
        hosts: [],
        host_suffixes: [],
        requires_track_link: true,
      },
      {
        key: 'youtubeMusic',
        display_name: 'YouTube Music',
        hosts: [],
        host_suffixes: [],
        requires_track_link: true,
      },
    ],
    release_types: [
      { value: 'single', label: 'Сингл' },
      { value: 'album', label: 'Альбом' },
      { value: 'ep', label: 'Міні-альбом' },
    ],
    languages: ['українська', 'англійська'],
    genres: [{ name: 'Рок', default_hashtag: 'Рок' }],
    manual_field_names: [
      'title',
      'artists',
      'year',
      'release_type',
      'release_title',
      'album_id_spotify',
    ],
    caption_limit: 1024,
  }
}

export type FragmentView = Schemas['FragmentView']

// a microsecond stamp: it has to reach the server exactly as the server sent it
export const MICRO_STAMP = '2026-10-09T12:34:56.123456Z'

export function makeFragmentView(overrides: Partial<FragmentView> = {}): FragmentView {
  return {
    post_updated_at: MICRO_STAMP,
    start_ms: 0,
    end_ms: 45000,
    audio_duration_ms: 200000,
    max_length_ms: 120000,
    audio_url: '/api/posts/7/audio?token=first',
    audio_url_expires_at: '2026-10-09T12:49:56Z',
    peaks: { peaks_per_second: 20, values: [0, 128, 255, 64] },
    ...overrides,
  }
}

// a server with the editor's three reads wired to the given detail
export function editorServer(detail: PostDetail, extra: Record<string, Handler> = {}): FakeServer {
  return fakeServer({
    [`GET /api/posts/${detail.id}`]: () => jsonResponse(detail),
    'GET /api/meta': () => jsonResponse(makeMeta()),
    [`POST /api/posts/${detail.id}/preview`]: () =>
      jsonResponse({
        caption: makeCaption(),
        stored_caption_sha256: null,
        differs_from_stored: true,
      }),
    ...extra,
  })
}
