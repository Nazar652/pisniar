import { computed, ref, shallowRef } from 'vue'
import type { ApiClient } from '../api/client'
import { outcomeOf, type Failure, type Schemas } from '../api/outcome'
import {
  clampToRules,
  endAt,
  isWithinRules,
  nudge,
  snapBounds,
  startAt,
  type Bounds,
  type Edge,
  type Limits,
  type Moved,
} from './bounds'

export type FragmentView = Schemas['FragmentView']
export type FragmentSaved = Schemas['FragmentSaved']
export type FragmentStatus = 'loading' | 'ready' | 'saving' | 'submitting' | 'submitted' | 'failed'
export type FragmentEditorState = ReturnType<typeof useFragmentEditor>

export const SUBMITTED_MESSAGE = 'Рендер запущено. Нове прев’ю з’явиться в чаті, коли буде готове.'
export const SAVED_MESSAGE = 'Фрагмент збережено.'
export const SAVED_STALE_MESSAGE =
  'Фрагмент збережено. Прев’ю в чаті застаріло — зробіть відео заново, щоб опублікувати.'
export const MEDIA_ERROR_MESSAGE =
  'Не вдалося відтворити аудіо. Закрийте редактор і відкрийте його знову.'

// after these the post cannot be re-rendered from this screen; any other refusal leaves the
// editor open so the admin can adjust the bounds or try again
const DEAD_END_CODES = new Set([
  'not_choosable',
  'audio_missing',
  'dispatch_failed',
  'post_not_found',
])

// `beforeSubmit` answers false when the admin declines to go on, e.g. over other unsaved work;
// `onSaved` gets the answer and the stamp the save was sent with
export function useFragmentEditor(
  client: ApiClient,
  postId: number,
  onSubmitted?: () => void,
  beforeSubmit?: () => Promise<boolean>,
  onSaved?: (saved: FragmentSaved, sentStamp: string) => void,
) {
  const status = ref<FragmentStatus>('loading')
  const message = ref('')
  const messageKind = ref<'success' | 'error'>('error')
  const view = shallowRef<FragmentView | null>(null)
  const bounds = ref<Bounds>({ startMs: 0, endMs: 1 })
  // what the server holds: the view's bounds until a save moves them (the view itself stays, so the waveform is not rebuilt)
  const stored = ref<Bounds>({ startMs: 0, endMs: 1 })
  const audioUrl = ref('')

  // the server's own string: a JS `Date` would cut the microseconds the stale check compares
  let expectedUpdatedAt = ''
  let mediaRefreshed = false

  const limits = computed<Limits | null>(() =>
    view.value === null
      ? null
      : { durationMs: view.value.audio_duration_ms, maxLengthMs: view.value.max_length_ms },
  )
  const canSubmit = computed(
    () =>
      status.value === 'ready' &&
      limits.value !== null &&
      isWithinRules(bounds.value, limits.value),
  )

  // chosen bounds that differ from the server's, before anything is saved or submitted
  const dirty = computed(
    () =>
      status.value === 'ready' &&
      view.value !== null &&
      (bounds.value.startMs !== stored.value.startMs || bounds.value.endMs !== stored.value.endMs),
  )
  const canSave = computed(() => canSubmit.value && dirty.value)

  function say(kind: 'success' | 'error', text: string): void {
    messageKind.value = kind
    message.value = text
  }

  // a sentence about the last save or submit is not true of bounds moved since
  function clearSuccess(): void {
    if (messageKind.value === 'success' && status.value === 'ready') message.value = ''
  }

  function fetchView() {
    return outcomeOf(
      client.GET('/api/posts/{post_id}/fragment', { params: { path: { post_id: postId } } }),
    )
  }

  async function load(): Promise<boolean> {
    status.value = 'loading'
    say('error', '')
    const answer = await fetchView()
    if (!answer.ok) {
      status.value = 'failed'
      say('error', answer.failure.message)
      return false
    }
    view.value = answer.data
    bounds.value = { startMs: answer.data.start_ms, endMs: answer.data.end_ms }
    stored.value = { ...bounds.value }
    audioUrl.value = answer.data.audio_url
    expectedUpdatedAt = answer.data.post_updated_at
    mediaRefreshed = false
    status.value = 'ready'
    return true
  }

  // the post changed elsewhere (a field save) and the screen read it again: the next submit carries that stamp,
  // so it is not refused as stale and the dragged bounds stay
  function adoptStamp(stamp: string): void {
    if (view.value !== null) expectedUpdatedAt = stamp
  }

  // a drag in progress is only kept inside the rules; a drop also lands on the 100 ms grid
  function moveTo(next: Bounds, moved: Moved, dropped: boolean): void {
    const current = limits.value
    if (current === null || status.value !== 'ready') return
    clearSuccess()
    bounds.value = dropped ? snapBounds(next, current, moved) : clampToRules(next, current, moved)
  }

  function nudgeEdge(edge: Edge, deltaMs: number): void {
    const current = limits.value
    if (current === null || status.value !== 'ready') return
    clearSuccess()
    bounds.value = nudge(bounds.value, edge, deltaMs, current)
  }

  // the playhead sets an edge; the position is the audio's, in seconds
  function setEdgeAt(edge: Edge, seconds: number): void {
    const current = limits.value
    if (current === null || status.value !== 'ready') return
    clearSuccess()
    const ms = Math.round(seconds * 1000)
    bounds.value =
      edge === 'start' ? startAt(bounds.value, ms, current) : endAt(bounds.value, ms, current)
  }

  async function fail(failure: Failure): Promise<void> {
    if (failure.code === 'stale_data') {
      if (await load()) say('error', failure.message)
      return
    }
    say('error', failure.message)
    status.value = DEAD_END_CODES.has(failure.code) ? 'failed' : 'ready'
  }

  // stores the bounds without a render; the view and the waveform stay, only the stamp and the stored bounds move
  async function save(): Promise<void> {
    if (!canSave.value) return
    status.value = 'saving'
    say('error', '')
    const sentStamp = expectedUpdatedAt
    const sent = { ...bounds.value }
    const answer = await outcomeOf(
      client.PUT('/api/posts/{post_id}/fragment', {
        params: { path: { post_id: postId } },
        body: { start_ms: sent.startMs, end_ms: sent.endMs, expected_post_updated_at: sentStamp },
      }),
    )
    if (!answer.ok) {
      await fail(answer.failure)
      return
    }
    expectedUpdatedAt = answer.data.post_updated_at
    stored.value = sent
    status.value = 'ready'
    say('success', answer.data.preview_stale ? SAVED_STALE_MESSAGE : SAVED_MESSAGE)
    onSaved?.(answer.data, sentStamp)
  }

  async function submit(): Promise<void> {
    if (!canSubmit.value) return
    if (beforeSubmit !== undefined && !(await beforeSubmit())) return
    if (!canSubmit.value) return
    status.value = 'submitting'
    say('error', '')
    const answer = await outcomeOf(
      client.POST('/api/posts/{post_id}/fragment', {
        params: { path: { post_id: postId } },
        body: {
          start_ms: bounds.value.startMs,
          end_ms: bounds.value.endMs,
          expected_post_updated_at: expectedUpdatedAt,
        },
      }),
    )
    if (!answer.ok) {
      await fail(answer.failure)
      return
    }
    status.value = 'submitted'
    say('success', SUBMITTED_MESSAGE)
    onSubmitted?.()
  }

  // the signed URL lives 15 minutes; one fresh view is asked for, then the admin gets a sentence
  async function refreshMedia(): Promise<boolean> {
    if (mediaRefreshed) {
      say('error', MEDIA_ERROR_MESSAGE)
      return false
    }
    mediaRefreshed = true
    const answer = await fetchView()
    if (!answer.ok) {
      say('error', answer.failure.message)
      return false
    }
    audioUrl.value = answer.data.audio_url
    return true
  }

  // playback worked, so a later expiry may ask for a fresh URL again
  function mediaPlayable(): void {
    mediaRefreshed = false
  }

  return {
    status,
    message,
    messageKind,
    view,
    bounds,
    audioUrl,
    limits,
    canSubmit,
    canSave,
    dirty,
    load,
    adoptStamp,
    moveTo,
    nudgeEdge,
    setEdgeAt,
    save,
    submit,
    refreshMedia,
    mediaPlayable,
  }
}
