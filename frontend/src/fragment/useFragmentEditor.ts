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
export type FragmentStatus = 'loading' | 'ready' | 'submitting' | 'submitted' | 'failed'
export type FragmentEditorState = ReturnType<typeof useFragmentEditor>

export const SUBMITTED_MESSAGE = 'Рендер запущено. Нове прев’ю з’явиться в чаті, коли буде готове.'
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

// `beforeSubmit` answers false when the admin declines to go on, e.g. over other unsaved work
export function useFragmentEditor(
  client: ApiClient,
  postId: number,
  onSubmitted?: () => void,
  beforeSubmit?: () => Promise<boolean>,
) {
  const status = ref<FragmentStatus>('loading')
  const message = ref('')
  const view = shallowRef<FragmentView | null>(null)
  const bounds = ref<Bounds>({ startMs: 0, endMs: 1 })
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

  // chosen bounds that differ from the server's, before anything is submitted
  const dirty = computed(
    () =>
      status.value === 'ready' &&
      view.value !== null &&
      (bounds.value.startMs !== view.value.start_ms || bounds.value.endMs !== view.value.end_ms),
  )

  function fetchView() {
    return outcomeOf(
      client.GET('/api/posts/{post_id}/fragment', { params: { path: { post_id: postId } } }),
    )
  }

  async function load(): Promise<boolean> {
    status.value = 'loading'
    message.value = ''
    const answer = await fetchView()
    if (!answer.ok) {
      status.value = 'failed'
      message.value = answer.failure.message
      return false
    }
    view.value = answer.data
    bounds.value = { startMs: answer.data.start_ms, endMs: answer.data.end_ms }
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
    bounds.value = dropped ? snapBounds(next, current, moved) : clampToRules(next, current, moved)
  }

  function nudgeEdge(edge: Edge, deltaMs: number): void {
    const current = limits.value
    if (current === null || status.value !== 'ready') return
    bounds.value = nudge(bounds.value, edge, deltaMs, current)
  }

  // the playhead sets an edge; the position is the audio's, in seconds
  function setEdgeAt(edge: Edge, seconds: number): void {
    const current = limits.value
    if (current === null || status.value !== 'ready') return
    const ms = Math.round(seconds * 1000)
    bounds.value =
      edge === 'start' ? startAt(bounds.value, ms, current) : endAt(bounds.value, ms, current)
  }

  async function fail(failure: Failure): Promise<void> {
    if (failure.code === 'stale_data') {
      if (await load()) message.value = failure.message
      return
    }
    message.value = failure.message
    status.value = DEAD_END_CODES.has(failure.code) ? 'failed' : 'ready'
  }

  async function submit(): Promise<void> {
    if (!canSubmit.value) return
    if (beforeSubmit !== undefined && !(await beforeSubmit())) return
    if (!canSubmit.value) return
    status.value = 'submitting'
    message.value = ''
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
    message.value = SUBMITTED_MESSAGE
    onSubmitted?.()
  }

  // the signed URL lives 15 minutes; one fresh view is asked for, then the admin gets a sentence
  async function refreshMedia(): Promise<boolean> {
    if (mediaRefreshed) {
      message.value = MEDIA_ERROR_MESSAGE
      return false
    }
    mediaRefreshed = true
    const answer = await fetchView()
    if (!answer.ok) {
      message.value = answer.failure.message
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
    view,
    bounds,
    audioUrl,
    limits,
    canSubmit,
    dirty,
    load,
    adoptStamp,
    moveTo,
    nudgeEdge,
    setEdgeAt,
    submit,
    refreshMedia,
    mediaPlayable,
  }
}
