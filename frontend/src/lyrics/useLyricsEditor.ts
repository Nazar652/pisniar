import { computed, ref, shallowRef } from 'vue'
import type { ApiClient } from '../api/client'
import { outcomeOf, type Failure, type Schemas } from '../api/outcome'
import type { Confirm } from '../telegram/actions'
import {
  bufferOf,
  deleteLine,
  glassAt,
  insertLine,
  insertMusicMark,
  insertPause,
  linesFromPaste,
  linesLabel,
  compensatedMs,
  nextUntimed,
  nudgeLine,
  retimeFrom,
  sameLines,
  setText,
  slotStartMs,
  tap,
  tapLabel,
  timedCount,
  undo,
  validate,
  NOT_PLAYING_MESSAGE,
  type Line,
  type LyricsBuffer,
  type Patch,
  type Problem,
  type Step,
  type TextLimits,
} from './timing'

export type LyricsView = Schemas['LyricsView']
export type FragmentView = Schemas['FragmentView']
export type LyricsProposal = Schemas['LyricsProposal']
export type PlainSource = LyricsProposal['source']
export type LyricsStatus = 'loading' | 'ready' | 'timing' | 'saving' | 'saved' | 'failed'
export type MessageArea = 'tap' | 'save'
export type LyricsEditorState = ReturnType<typeof useLyricsEditor>

export interface LyricsDeps {
  confirm: Confirm
  // called after the text is stored; `rendered` says whether a render was queued with it
  onSaved?: (rendered: boolean) => void
  // asked before a save that queues a render; false when the admin declines, e.g. over other unsaved work
  beforeRender?: () => Promise<boolean>
}

export const PLAIN_SOURCES: { key: PlainSource; label: string }[] = [
  { key: 'genius', label: 'Genius' },
  { key: 'ytmusic', label: 'YouTube Music' },
  { key: 'lrclib', label: 'LRCLIB' },
]
export const DEFAULT_PLAIN_SOURCE: PlainSource = 'genius'

export const SAVED_MESSAGE = 'Текст збережено.'
export const RENDER_STARTED_MESSAGE =
  'Текст збережено, рендер запущено. Нове прев’ю з’явиться в чаті, коли буде готове.'
export const RENDER_ONLY_MESSAGE =
  'Рендер запущено. Нове прев’ю з’явиться в чаті, коли буде готове.'
export const MEDIA_ERROR_MESSAGE =
  'Не вдалося відтворити аудіо. Закрийте редактор і відкрийте його знову.'
export const KEEP_OR_LOAD_QUESTION =
  'Збережений текст змінився. «ОК» — залишити ваш текст і зберегти його поверх, «Скасувати» — завантажити збережений.'
export const NO_MORE_UNTIMED_MESSAGE = 'Невідстуканих рядків далі немає.'
export const EMPTY_PROPOSAL_MESSAGE = 'Знайдений текст порожній.'

// after these nothing can be saved from this screen until it is opened again
const DEAD_END_CODES = new Set(['not_choosable', 'audio_missing', 'post_not_found'])

// the audio url is a path on this server, never another origin
export function isMediaPath(url: string): boolean {
  return url.startsWith('/api/') && !url.includes('\\')
}

function sourceLabel(source: PlainSource): string {
  return PLAIN_SOURCES.find((entry) => entry.key === source)?.label ?? source
}

function linesOfView(view: LyricsView): Line[] {
  return view.lines.map((line) => ({ text: line.text, startMs: line.start_ms }))
}

export function useLyricsEditor(client: ApiClient, postId: number, deps: LyricsDeps) {
  const status = ref<LyricsStatus>('loading')
  const message = ref('')
  const failed = ref(false)
  const area = ref<MessageArea>('save')
  const view = shallowRef<LyricsView | null>(null)
  const fragment = shallowRef<FragmentView | null>(null)
  const buffer = shallowRef<LyricsBuffer>({ lines: [], cursor: 0 })
  const saved = shallowRef<readonly Line[]>([])
  const speed = ref(1)
  const plainSource = ref<PlainSource>(DEFAULT_PLAIN_SOURCE)
  const looking = ref(false)
  const needsReload = ref(false)
  const audioUrl = ref('')
  // whether the audio plays: the gate of a tap, whatever the status shows
  const playing = ref(false)
  // where the audio is, in whole milliseconds of the full track
  const positionMs = ref(0)
  const history = shallowRef<{ patch: Patch; rewindMs: number | null }[]>([])

  // the server's own string: a JS `Date` would cut the microseconds the stale check compares
  let expectedUpdatedAt = ''
  let mediaRefreshed = false

  const limits = computed<TextLimits | null>(() =>
    view.value === null || fragment.value === null
      ? null
      : {
          maxLines: view.value.max_lines,
          maxLineLength: view.value.max_line_length,
          maxTotalCharacters: view.value.max_total_characters,
          durationMs: fragment.value.audio_duration_ms,
        },
  )
  const lines = computed(() => buffer.value.lines)
  const dirty = computed(() => !sameLines(buffer.value.lines, saved.value))
  const problem = computed<Problem | null>(() =>
    limits.value === null ? null : validate(buffer.value.lines, limits.value),
  )
  const timed = computed(() => timedCount(buffer.value.lines))
  // the video's glass at the audio position
  const glass = computed(() => glassAt(buffer.value.lines, positionMs.value))
  const soundingRow = computed(() => (playing.value ? glass.value.active : null))
  // the row kept in view while the audio plays; pausing leaves the list where it is
  const followedRow = computed(() => (playing.value ? glass.value.active : null))
  const tapText = computed(() => tapLabel(buffer.value))
  const canUndo = computed(() => history.value.length > 0)
  const busy = computed(() => status.value === 'saving' || looking.value)
  const editable = computed(
    () => view.value !== null && !needsReload.value && status.value !== 'loading' && !busy.value,
  )
  const canRender = computed(
    () => editable.value && limits.value !== null && problem.value === null,
  )
  const canSave = computed(() => canRender.value && dirty.value)

  // timing sentences show next to the tap button, the rest next to the save buttons
  function say(text: string, isFailure: boolean, near: MessageArea = 'save'): void {
    message.value = text
    failed.value = isFailure
    area.value = near
  }

  function fetchLyrics() {
    return outcomeOf(
      client.GET('/api/posts/{post_id}/lyrics', { params: { path: { post_id: postId } } }),
    )
  }

  function fetchFragment() {
    return outcomeOf(
      client.GET('/api/posts/{post_id}/fragment', { params: { path: { post_id: postId } } }),
    )
  }

  function adopt(next: LyricsView): void {
    view.value = next
    saved.value = linesOfView(next)
    buffer.value = bufferOf(saved.value)
    history.value = []
  }

  async function load(): Promise<boolean> {
    status.value = 'loading'
    say('', false)
    needsReload.value = false
    const [text, audio] = await Promise.all([fetchLyrics(), fetchFragment()])
    if (!text.ok || !audio.ok) {
      const failure = !text.ok ? text.failure : audio.ok ? null : audio.failure
      status.value = 'failed'
      needsReload.value = true
      say(failure?.message ?? '', true)
      return false
    }
    fragment.value = audio.data
    const mediaOk = isMediaPath(audio.data.audio_url)
    audioUrl.value = mediaOk ? audio.data.audio_url : ''
    expectedUpdatedAt = audio.data.post_updated_at
    mediaRefreshed = false
    adopt(text.data)
    status.value = restingStatus()
    if (!mediaOk) say(MEDIA_ERROR_MESSAGE, true)
    return true
  }

  // the state of an editor at work: timing while the audio plays, ready otherwise
  function restingStatus(): LyricsStatus {
    return playing.value ? 'timing' : 'ready'
  }

  // an edit leaves a finished save behind and takes the editor back to work
  function touched(): void {
    if (status.value === 'saved' || status.value === 'failed') status.value = restingStatus()
  }

  function apply(
    step: Step,
    rewindMs: number | null,
    stacked: boolean,
    near: MessageArea = 'tap',
  ): boolean {
    if (!step.ok) {
      say(step.message, true, near)
      return false
    }
    buffer.value = step.buffer
    history.value = stacked ? [...history.value, { patch: step.patch, rewindMs }] : []
    say('', false)
    touched()
    return true
  }

  // playing is what makes a tap meaningful, so the state follows the audio
  function setPlaying(isPlaying: boolean): void {
    playing.value = isPlaying
    const idle = status.value === 'ready' || status.value === 'saved' || status.value === 'failed'
    if (isPlaying && idle && view.value !== null && !needsReload.value) status.value = 'timing'
    else if (!isPlaying && status.value === 'timing') status.value = 'ready'
  }

  function setSpeed(next: number): void {
    speed.value = next
  }

  function setPosition(ms: number): void {
    positionMs.value = ms
  }

  // the time a slot's line starts at, for the audio to go to; neither plays nor pauses
  function slotStart(index: number | null): number | null {
    return slotStartMs(buffer.value.lines, index)
  }

  function tapAt(positionMs: number): boolean {
    const current = limits.value
    if (!editable.value || current === null) return false
    if (!playing.value) {
      say(NOT_PLAYING_MESSAGE, true, 'tap')
      return false
    }
    const step = tap(buffer.value, positionMs, speed.value, current.durationMs)
    return apply(step, compensatedMs(positionMs, speed.value), true)
  }

  // takes the last change back; the answer is where the audio should go, if the change had a time
  function undoLast(): number | null {
    const last = history.value[history.value.length - 1]
    if (last === undefined || !editable.value) return null
    buffer.value = undo(buffer.value, last.patch)
    history.value = history.value.slice(0, -1)
    say('', false)
    touched()
    return last.rewindMs
  }

  function nudge(index: number, deltaMs: number): boolean {
    const current = limits.value
    if (!editable.value || current === null) return false
    return apply(nudgeLine(buffer.value, index, deltaMs, current.durationMs), null, true)
  }

  // the cursor goes to the next line that still has no time
  function toNextUntimed(): boolean {
    if (!editable.value) return false
    const index = nextUntimed(buffer.value)
    if (index === null) {
      say(NO_MORE_UNTIMED_MESSAGE, true, 'tap')
      return false
    }
    buffer.value = retimeFrom(buffer.value, index)
    say('', false)
    return true
  }

  function retime(index: number): void {
    if (!editable.value) return
    buffer.value = retimeFrom(buffer.value, index)
    say('', false)
  }

  function edit(index: number, text: string): void {
    if (!editable.value) return
    buffer.value = setText(buffer.value, index, text)
    history.value = []
    touched()
  }

  function addLine(at: number): boolean {
    const current = limits.value
    if (!editable.value || current === null) return false
    return apply(insertLine(buffer.value, at, current), null, false, 'save')
  }

  function removeLine(index: number): boolean {
    if (!editable.value) return false
    return apply(deleteLine(buffer.value, index), null, false, 'save')
  }

  function addPause(positionMs: number): boolean {
    const current = limits.value
    if (!editable.value || current === null) return false
    const ms = compensatedMs(positionMs, speed.value)
    return apply(insertPause(buffer.value, ms, current), ms, true)
  }

  function addMusicMark(positionMs: number): boolean {
    const current = limits.value
    if (!editable.value || current === null) return false
    const ms = compensatedMs(positionMs, speed.value)
    return apply(insertMusicMark(buffer.value, ms, current), ms, true)
  }

  function replaceBuffer(next: readonly Line[]): void {
    buffer.value = bufferOf(next)
    history.value = []
    touched()
  }

  // pasted text replaces the list, and a list that was timed is replaced only on a yes
  async function paste(text: string): Promise<boolean> {
    if (!editable.value) return false
    const pasted = linesFromPaste(text)
    if (pasted.length === 0) return false
    if (timed.value > 0) {
      const count = linesLabel(buffer.value.lines.length)
      const sure = await deps.confirm(
        `Замінити поточний текст (${count}, із часом) вставленим (${linesLabel(pasted.length)})?`,
      )
      if (!sure) return false
    }
    replaceBuffer(pasted)
    say('', false)
    return true
  }

  function proposalQuestion(proposal: LyricsProposal): string {
    const current = linesLabel(buffer.value.lines.length)
    const found = linesLabel(proposal.lines.length)
    const label = sourceLabel(proposal.source)
    return proposal.kind === 'synced'
      ? `Замінити поточний текст (${current}) знайденим (${found} із часом, ${label})?`
      : `Замінити поточний текст (${current}) текстом із ${label} (${found} без часу)?`
  }

  function proposalNote(proposal: LyricsProposal): string {
    const parts: string[] = []
    if (proposal.dropped_line_count > 0) {
      parts.push(`пропущено ${linesLabel(proposal.dropped_line_count)}`)
    }
    if (proposal.truncated_line_count > 0) {
      parts.push(`скорочено ${linesLabel(proposal.truncated_line_count)}`)
    }
    return parts.length === 0 ? '' : ` (${parts.join(', ')})`
  }

  // a proposal is only read; the buffer changes after the admin has said yes
  async function lookup(kind: 'synced' | 'plain'): Promise<boolean> {
    if (!editable.value) return false
    looking.value = true
    say('', false)
    const answer = await outcomeOf(
      kind === 'synced'
        ? client.GET('/api/posts/{post_id}/lyrics/synced', {
            params: { path: { post_id: postId } },
          })
        : client.GET('/api/posts/{post_id}/lyrics/plain', {
            params: { path: { post_id: postId }, query: { source: plainSource.value } },
          }),
    )
    try {
      if (!answer.ok) {
        if (DEAD_END_CODES.has(answer.failure.code)) needsReload.value = true
        say(answer.failure.message, true)
        return false
      }
      const proposal = answer.data
      if (proposal.lines.length === 0) {
        say(EMPTY_PROPOSAL_MESSAGE, true)
        return false
      }
      if (buffer.value.lines.length > 0 && !(await deps.confirm(proposalQuestion(proposal)))) {
        return false
      }
      replaceBuffer(proposal.lines.map((line) => ({ text: line.text, startMs: line.start_ms })))
      say(
        `Текст підставлено: ${linesLabel(proposal.lines.length)}${proposalNote(proposal)}. Не забудьте зберегти.`,
        false,
      )
      return true
    } finally {
      looking.value = false
    }
  }

  const findSynced = () => lookup('synced')
  const findPlain = () => lookup('plain')

  function setPlainSource(next: PlainSource): void {
    plainSource.value = next
  }

  // the stored text changed under the admin: keep the buffer on a yes, load the stored text otherwise
  async function resolveStale(sentence: string): Promise<void> {
    if (!(await deps.confirm(KEEP_OR_LOAD_QUESTION))) {
      if (await load()) say(sentence, true)
      return
    }
    const fresh = await fetchLyrics()
    if (!fresh.ok) {
      say(fresh.failure.message, true)
      return
    }
    view.value = fresh.data
    saved.value = linesOfView(fresh.data)
    status.value = restingStatus()
    say(sentence, true)
  }

  async function failSave(failure: Failure): Promise<void> {
    status.value = 'failed'
    if (failure.code === 'stale_data') {
      await resolveStale(failure.message)
      return
    }
    if (DEAD_END_CODES.has(failure.code)) needsReload.value = true
    say(failure.message, true)
  }

  // the render takes the bounds the server showed; a refusal leaves the saved text as it is
  async function startRender(textWritten: boolean): Promise<boolean> {
    const current = fragment.value
    if (current === null) return false
    const answer = await outcomeOf(
      client.POST('/api/posts/{post_id}/fragment', {
        params: { path: { post_id: postId } },
        body: {
          start_ms: current.start_ms,
          end_ms: current.end_ms,
          expected_post_updated_at: expectedUpdatedAt,
        },
      }),
    )
    if (answer.ok) return true
    if (answer.failure.code === 'stale_data') {
      const fresh = await fetchFragment()
      if (fresh.ok) {
        fragment.value = fresh.data
        expectedUpdatedAt = fresh.data.post_updated_at
      }
    }
    say(textWritten ? `${SAVED_MESSAGE} ${answer.failure.message}` : answer.failure.message, true)
    return false
  }

  // with a render, text that is already stored is not written again
  async function save(thenRender = false): Promise<boolean> {
    const current = view.value
    if (current === null || !(thenRender ? canRender.value : canSave.value)) return false
    if (thenRender && deps.beforeRender !== undefined && !(await deps.beforeRender())) return false
    if (!(thenRender ? canRender.value : canSave.value)) return false
    status.value = 'saving'
    say('', false)
    const wrote = dirty.value
    if (wrote) {
      const answer = await outcomeOf(
        client.PUT('/api/posts/{post_id}/lyrics', {
          params: { path: { post_id: postId } },
          body: {
            expected_lyrics_version: current.lyrics_version,
            lines: buffer.value.lines.map((line) => ({ text: line.text, start_ms: line.startMs })),
          },
        }),
      )
      if (!answer.ok) {
        await failSave(answer.failure)
        return false
      }
      const cursor = buffer.value.cursor
      adopt(answer.data)
      buffer.value = {
        lines: buffer.value.lines,
        cursor: Math.min(cursor, buffer.value.lines.length),
      }
    }
    status.value = 'saved'
    say(SAVED_MESSAGE, false)
    const rendered = thenRender && (await startRender(wrote))
    if (rendered) say(wrote ? RENDER_STARTED_MESSAGE : RENDER_ONLY_MESSAGE, false)
    deps.onSaved?.(rendered)
    return true
  }

  // another editor saved bounds: the render carries them and their stamp, while the text, the audio URL and the
  // playback stay as they are; a refusal leaves the bounds held, and the render's own stale answer reloads them
  async function refreshFragment(): Promise<boolean> {
    if (view.value === null) return false
    const answer = await fetchFragment()
    if (!answer.ok) return false
    fragment.value = answer.data
    expectedUpdatedAt = answer.data.post_updated_at
    return true
  }

  // the signed URL lives 15 minutes; one fresh view is asked for, then the admin gets a sentence
  async function refreshMedia(): Promise<boolean> {
    if (mediaRefreshed) {
      say(MEDIA_ERROR_MESSAGE, true)
      return false
    }
    mediaRefreshed = true
    // a media error means the audio is not playing, whatever the element last said
    setPlaying(false)
    const answer = await fetchFragment()
    if (!answer.ok) {
      say(answer.failure.message, true)
      return false
    }
    if (!isMediaPath(answer.data.audio_url)) {
      say(MEDIA_ERROR_MESSAGE, true)
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
    failed,
    area,
    view,
    fragment,
    buffer,
    lines,
    playing,
    positionMs,
    glass,
    soundingRow,
    followedRow,
    tapText,
    speed,
    plainSource,
    looking,
    needsReload,
    audioUrl,
    limits,
    dirty,
    problem,
    timed,
    canUndo,
    busy,
    editable,
    canSave,
    canRender,
    load,
    setPlaying,
    setSpeed,
    setPosition,
    slotStart,
    tapAt,
    undoLast,
    nudge,
    retime,
    toNextUntimed,
    edit,
    addLine,
    removeLine,
    addPause,
    addMusicMark,
    paste,
    findSynced,
    findPlain,
    setPlainSource,
    save,
    refreshFragment,
    refreshMedia,
    mediaPlayable,
  }
}
