<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { LEAVE_GUARD } from '../app/leaveGuard'
import { MAIN_BUTTON_GATE } from '../app/mainButtonGate'
import NoticeBanner from '../components/NoticeBanner.vue'
import { inputValue } from '../editor/dom'
import type { Notice } from '../editor/usePostEditor'
import { formatTime } from '../fragment/bounds'
import { PLAYBACK_VOLUME } from '../fragment/waveform'
import { lockVerticalSwipes, setClosingConfirmation } from '../telegram/actions'
import type { WebApp } from '../telegram/useTelegram'
import { releaseEditor } from './cleanup'
import { blurAfterClick, blurButton } from './focus'
import { attachKeys } from './keys'
import {
  COARSE_NUDGE_MS,
  FINE_NUDGE_MS,
  PAUSE_LABEL,
  SPEEDS,
  inFragment,
  seekBeforeMs,
  type Line,
} from './timing'
import { PLAIN_SOURCES, type LyricsEditorState, type PlainSource } from './useLyricsEditor'

const props = defineProps<{
  editor: LyricsEditorState
  webApp: WebApp | undefined
}>()

const STEPS = [
  { label: '−1 с', deltaMs: -COARSE_NUDGE_MS },
  { label: '−0,1 с', deltaMs: -FINE_NUDGE_MS },
  { label: '+0,1 с', deltaMs: FINE_NUDGE_MS },
  { label: '+1 с', deltaMs: COARSE_NUDGE_MS },
]
const NO_LINE = '—'

const root = ref<HTMLElement | null>(null)
const audioElement = ref<HTMLAudioElement | null>(null)
const positionMs = props.editor.positionMs
const playing = ref(false)
const pasteText = ref('')

let restoreSwipes: () => void = () => undefined
let detachKeys: () => void = () => undefined
let releaseMainButton: () => void = () => undefined
let resumeAtMs: number | null = null

const durationMs = computed(() => props.editor.limits.value?.durationMs ?? 0)
const cursor = computed(() => props.editor.buffer.value.cursor)
const glass = props.editor.glass
const leaveGuard = inject(LEAVE_GUARD, null)
const mainButtonGate = inject(MAIN_BUTTON_GATE, null)

function noticeIn(area: 'tap' | 'save'): Notice | null {
  const { message, failed, area: current } = props.editor
  if (message.value === '' || current.value !== area) return null
  return { kind: failed.value ? 'error' : 'success', text: message.value }
}
const tapNotice = computed(() => noticeIn('tap'))
const saveNotice = computed(() => noticeIn('save'))
const locked = computed(() => !props.editor.editable.value)

function slotText(index: number | null): string {
  return index === null ? '' : (props.editor.lines.value[index]?.text ?? '')
}

function timeText(line: Line): string {
  return line.startMs === null ? NO_LINE : formatTime(line.startMs)
}

function inVideo(line: Line): boolean {
  const view = props.editor.fragment.value
  return view !== null && inFragment(line, view.start_ms, view.end_ms)
}

function nowMs(): number {
  return Math.round((audioElement.value?.currentTime ?? 0) * 1000)
}

function seek(ms: number): void {
  const media = audioElement.value
  if (media !== null) media.currentTime = ms / 1000
  props.editor.setPosition(ms)
}

// a slot takes the audio to its line and leaves playing or pausing as it was
function seekToSlot(index: number | null): void {
  const startMs = props.editor.slotStart(index)
  if (startMs !== null) seek(startMs)
}

function play(): void {
  void audioElement.value?.play().catch(() => undefined)
}

// only the primary button or finger taps; a second finger or a right click does not
function tapPointer(event: PointerEvent): void {
  blurButton(event.currentTarget)
  if (event.isPrimary && event.button === 0) tapNow()
}

function togglePlay(): void {
  const media = audioElement.value
  if (media === null) return
  if (media.paused) play()
  else media.pause()
}

function tapNow(): void {
  props.editor.tapAt(nowMs())
}

function undoNow(): void {
  const to = props.editor.undoLast()
  if (to !== null) seek(seekBeforeMs(to))
}

function playFrom(line: Line): void {
  if (line.startMs === null) return
  seek(line.startMs)
  play()
}

// taps go on from this line: the audio runs from a few seconds before it
function tapFrom(index: number, line: Line): void {
  props.editor.retime(index)
  if (line.startMs !== null) seek(seekBeforeMs(line.startMs))
  play()
}

function applyPlayback(): void {
  const media = audioElement.value
  if (media === null) return
  media.playbackRate = props.editor.speed.value
  media.preservesPitch = true
  media.volume = PLAYBACK_VOLUME
}

function onMetadata(): void {
  applyPlayback()
  if (resumeAtMs !== null) seek(resumeAtMs)
  resumeAtMs = null
}

function onTime(): void {
  props.editor.setPosition(nowMs())
}

function onPlay(): void {
  playing.value = true
  props.editor.setPlaying(true)
}

function onPause(): void {
  playing.value = false
  props.editor.setPlaying(false)
}

// one fresh signed URL is fetched and the audio goes on from where it stopped
async function recoverMedia(): Promise<void> {
  onPause()
  resumeAtMs = nowMs()
  if (!(await props.editor.refreshMedia())) resumeAtMs = null
}

function sourceOf(event: Event): PlainSource {
  const chosen = inputValue(event)
  return PLAIN_SOURCES.find((entry) => entry.key === chosen)?.key ?? props.editor.plainSource.value
}

async function pasteNow(): Promise<void> {
  if (await props.editor.paste(pasteText.value)) pasteText.value = ''
}

watch(() => props.editor.speed.value, applyPlayback)

watch(
  () => props.editor.dirty.value,
  (dirty) => {
    // with a guard the closing confirmation follows all sources together
    if (leaveGuard === null) setClosingConfirmation(props.webApp, dirty)
    else leaveGuard.setDirty('lyrics', dirty)
  },
  { immediate: true },
)

async function showRow(index: number): Promise<void> {
  await nextTick()
  root.value?.querySelector(`[data-row="${index}"]`)?.scrollIntoView({ block: 'nearest' })
}

// while the audio plays the sounding line stays in view, and it wins over the tap cursor
watch(props.editor.followedRow, (index) => {
  if (index !== null) void showRow(index)
})

// the line to tap next stays in view
watch(cursor, (index) => {
  if (!props.editor.playing.value) void showRow(index)
})

onMounted(() => {
  releaseMainButton = mainButtonGate?.hide() ?? releaseMainButton
  restoreSwipes = lockVerticalSwipes(props.webApp)
  detachKeys = attachKeys(window, { tap: tapNow, undo: undoNow })
  if (props.editor.view.value === null) void props.editor.load()
})

onBeforeUnmount(() => {
  releaseMainButton()
  leaveGuard?.setDirty('lyrics', false)
  releaseEditor({
    detachKeys,
    restoreSwipes,
    audio: audioElement.value,
    webApp: leaveGuard === null ? props.webApp : undefined,
    setPlaying: props.editor.setPlaying,
  })
})
</script>

<template>
  <div ref="root" class="lyrics-editor" @click="blurAfterClick($event)">
    <p v-if="editor.view.value === null && editor.status.value === 'loading'" class="muted">
      Завантаження…
    </p>
    <template v-else-if="editor.view.value === null">
      <p class="error" role="alert">{{ editor.message.value }}</p>
      <button type="button" class="btn" @click="editor.load()">Спробувати знову</button>
    </template>
    <template v-else>
      <audio
        ref="audioElement"
        :src="editor.audioUrl.value || undefined"
        preload="metadata"
        @loadedmetadata="onMetadata"
        @canplay="editor.mediaPlayable()"
        @timeupdate="onTime"
        @play="onPlay"
        @pause="onPause"
        @ended="onPause"
        @error="recoverMedia()"
      ></audio>

      <p v-if="editor.view.value.other_post_ids.length > 0" class="muted">
        Текст спільний з постами
        {{ editor.view.value.other_post_ids.map((id) => `#${id}`).join(', ') }}: їхні наступні
        рендери візьмуть новий текст.
      </p>

      <div class="lyrics-editor__slots" aria-live="off">
        <button
          type="button"
          class="lyrics-editor__slot lyrics-editor__slot--side"
          :disabled="glass.previous === null"
          @click="seekToSlot(glass.previous)"
        >
          {{ slotText(glass.previous) }}
        </button>
        <button
          type="button"
          class="lyrics-editor__slot lyrics-editor__slot--current"
          :disabled="glass.current === null"
          @click="seekToSlot(glass.current)"
        >
          {{ slotText(glass.current) }}
        </button>
        <button
          type="button"
          class="lyrics-editor__slot lyrics-editor__slot--side"
          :disabled="glass.next === null"
          @click="seekToSlot(glass.next)"
        >
          {{ slotText(glass.next) }}
        </button>
      </div>

      <div class="lyrics-editor__player">
        <button type="button" class="btn" :disabled="locked" @click="togglePlay()">
          {{ playing ? 'Пауза' : 'Грати' }}
        </button>
        <span class="lyrics-editor__clock"
          >{{ formatTime(positionMs) }} / {{ formatTime(durationMs) }}</span
        >
        <span class="lyrics-editor__speeds">
          <button
            v-for="rate in SPEEDS"
            :key="rate"
            type="button"
            :class="['btn', 'btn--small', { 'btn--quiet': editor.speed.value !== rate }]"
            :aria-pressed="editor.speed.value === rate"
            @click="editor.setSpeed(rate)"
          >
            {{ String(rate).replace('.', ',') }}×
          </button>
        </span>
      </div>
      <input
        type="range"
        class="lyrics-editor__slider"
        min="0"
        :max="durationMs"
        step="100"
        :value="positionMs"
        aria-label="Позиція в треку"
        @input="seek(Number(inputValue($event)))"
      />

      <button
        type="button"
        class="btn lyrics-editor__tap"
        :disabled="locked"
        @pointerdown.prevent="tapPointer($event)"
      >
        <span class="lyrics-editor__tap-label">{{ editor.tapText.value }}</span>
      </button>
      <NoticeBanner :notice="tapNotice" />
      <div class="lyrics-editor__tools">
        <button
          type="button"
          class="btn btn--quiet"
          :disabled="locked || !editor.canUndo.value"
          @click="undoNow()"
        >
          Скасувати
        </button>
        <button
          type="button"
          class="btn btn--quiet"
          :disabled="locked"
          aria-label="Вставити ♪ у поточній позиції"
          @click="editor.addMusicMark(nowMs())"
        >
          ♪
        </button>
        <button
          type="button"
          class="btn btn--quiet"
          :disabled="locked"
          @click="editor.addPause(nowMs())"
        >
          Програш
        </button>
        <button
          type="button"
          class="btn btn--quiet"
          :disabled="locked"
          @click="editor.toNextUntimed()"
        >
          До наступного невідстуканого
        </button>
      </div>
      <p class="muted">
        Пробіл — тап, Backspace — скасувати. Поки курсор у полі тексту, клавіші друкують.
      </p>
      <p class="muted">
        Відстукано {{ editor.timed.value }} із {{ editor.lines.value.length }}. Позначка «у відео»
        стоїть біля рядків, що потраплять у вибраний фрагмент.
      </p>

      <ol class="lyrics-editor__lines">
        <li
          v-for="(line, index) in editor.lines.value"
          :key="index"
          :data-row="index"
          :class="[
            'lyrics-editor__row',
            {
              'lyrics-editor__row--active': index === cursor,
              'lyrics-editor__row--sounding': index === editor.soundingRow.value,
            },
          ]"
        >
          <div class="lyrics-editor__line">
            <button
              type="button"
              class="btn btn--small btn--quiet"
              :disabled="locked"
              :aria-label="`Відстукувати з рядка ${index + 1}`"
              @click="editor.retime(index)"
            >
              {{ index + 1 }}
            </button>
            <span :class="['lyrics-editor__time', { 'lyrics-editor__time--in': inVideo(line) }]">
              {{ timeText(line) }}
            </span>
            <span v-if="inVideo(line)" class="lyrics-editor__badge">у відео</span>
            <input
              class="input"
              :value="line.text"
              :placeholder="PAUSE_LABEL"
              :disabled="locked"
              :aria-label="`Текст рядка ${index + 1}`"
              @input="editor.edit(index, inputValue($event))"
            />
          </div>
          <div v-if="index === cursor" class="lyrics-editor__actions">
            <button
              type="button"
              class="btn btn--small"
              :disabled="locked || line.startMs === null"
              @click="playFrom(line)"
            >
              Грати з цього рядка
            </button>
            <button
              type="button"
              class="btn btn--small"
              :disabled="locked"
              @click="tapFrom(index, line)"
            >
              Відстукати звідси
            </button>
            <button
              v-for="step in STEPS"
              :key="step.label"
              type="button"
              class="btn btn--small"
              :disabled="locked || line.startMs === null"
              @click="editor.nudge(index, step.deltaMs)"
            >
              {{ step.label }}
            </button>
            <button
              type="button"
              class="btn btn--small"
              :disabled="locked"
              @click="editor.addLine(index + 1)"
            >
              Додати рядок
            </button>
            <button
              type="button"
              class="btn btn--small btn--danger"
              :disabled="locked"
              @click="editor.removeLine(index)"
            >
              Видалити
            </button>
          </div>
        </li>
      </ol>
      <button
        type="button"
        class="btn btn--quiet"
        :disabled="locked"
        @click="editor.addLine(editor.lines.value.length)"
      >
        Додати рядок у кінець
      </button>

      <div class="lyrics-editor__paste">
        <textarea
          v-model="pasteText"
          class="input"
          rows="4"
          :disabled="locked"
          placeholder="Вставте текст: кожен рядок стане рядком без часу"
          aria-label="Вставити текст"
        ></textarea>
        <button
          type="button"
          class="btn"
          :disabled="locked || pasteText.trim() === ''"
          @click="pasteNow()"
        >
          Вставити текст
        </button>
      </div>

      <div class="lyrics-editor__lookup">
        <button type="button" class="btn" :disabled="locked" @click="editor.findSynced()">
          Знайти синхронізацію
        </button>
        <span class="lyrics-editor__plain">
          <select
            class="input"
            :disabled="locked"
            aria-label="Джерело простого тексту"
            @change="editor.setPlainSource(sourceOf($event))"
          >
            <option
              v-for="entry in PLAIN_SOURCES"
              :key="entry.key"
              :value="entry.key"
              :selected="entry.key === editor.plainSource.value"
            >
              {{ entry.label }}
            </option>
          </select>
          <button type="button" class="btn" :disabled="locked" @click="editor.findPlain()">
            Знайти просто текст
          </button>
        </span>
        <p v-if="editor.looking.value" class="muted" role="status">Шукаємо текст…</p>
      </div>

      <p v-if="editor.problem.value" class="error" role="alert">
        {{ editor.problem.value.message }}
      </p>
      <NoticeBanner :notice="saveNotice" />

      <div class="lyrics-editor__save">
        <button
          type="button"
          class="btn"
          :disabled="!editor.canSave.value"
          @click="editor.save(false)"
        >
          {{ editor.status.value === 'saving' ? 'Збереження…' : 'Зберегти' }}
        </button>
        <button
          type="button"
          class="btn"
          :disabled="!editor.canRender.value"
          @click="editor.save(true)"
        >
          Зберегти й зробити відео
        </button>
      </div>
      <button
        v-if="editor.needsReload.value"
        type="button"
        class="btn btn--quiet"
        @click="editor.load()"
      >
        Оновити
      </button>
    </template>
  </div>
</template>

<style>
.lyrics-editor__slots {
  margin: 8px 0;
  text-align: center;
}

.lyrics-editor__slot {
  display: block;
  width: 100%;
  min-height: 1.4em;
  margin: 4px 0;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font-family: inherit;
  text-align: center;
  overflow-wrap: anywhere;
}

.lyrics-editor__slot:disabled {
  cursor: default;
}

.lyrics-editor__slot--side {
  color: var(--page-hint);
  font-size: 14px;
}

.lyrics-editor__slot--current {
  font-size: 20px;
  font-weight: 600;
}

.lyrics-editor__player {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.lyrics-editor__clock {
  font-variant-numeric: tabular-nums;
}

.lyrics-editor__speeds {
  display: flex;
  gap: 4px;
  margin-left: auto;
}

.lyrics-editor__slider {
  width: 100%;
  margin: 8px 0;
}

.lyrics-editor__tap {
  width: 100%;
  min-height: 64px;
  font-size: 20px;
  touch-action: manipulation;
  user-select: none;
}

.lyrics-editor__tap-label {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lyrics-editor__tools {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin: 6px 0;
}

.lyrics-editor__lines {
  margin: 8px 0;
  padding: 0;
  list-style: none;
}

.lyrics-editor__row {
  padding: 4px 0;
  border-bottom: 1px solid var(--page-bg);
}

.lyrics-editor__row--active {
  border-left: 3px solid var(--page-button);
  padding-left: 6px;
}

.lyrics-editor__row--sounding {
  background: var(--page-card);
}

.lyrics-editor__line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.lyrics-editor__line > .input {
  flex: 1 1 0;
  min-width: 0;
}

.lyrics-editor__time {
  min-width: 52px;
  color: var(--page-hint);
  font-variant-numeric: tabular-nums;
}

.lyrics-editor__time--in {
  color: var(--page-text);
  font-weight: 700;
}

.lyrics-editor__badge {
  color: var(--page-ok);
  font-size: 12px;
  white-space: nowrap;
}

.lyrics-editor__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}

.lyrics-editor__paste,
.lyrics-editor__lookup {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 10px 0;
}

.lyrics-editor__plain {
  display: flex;
  gap: 6px;
}

.lyrics-editor__plain > .input {
  flex: 0 0 auto;
  width: auto;
}

.lyrics-editor__save {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
</style>
