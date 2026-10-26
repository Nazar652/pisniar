<script setup lang="ts">
import { computed, inject, markRaw, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import WaveSurfer from 'wavesurfer.js'
import RegionsPlugin, { type Region } from 'wavesurfer.js/plugins/regions'
import { LEAVE_GUARD } from '../app/leaveGuard'
import NoticeBanner from '../components/NoticeBanner.vue'
import type { Notice } from '../editor/usePostEditor'
import { lockVerticalSwipes, setClosingConfirmation } from '../telegram/actions'
import type { WebApp } from '../telegram/useTelegram'
import {
  COARSE_NUDGE_MS,
  FINE_NUDGE_MS,
  SNAP_MS,
  formatTime,
  regionStateOf,
  themeColorsOf,
  type Bounds,
  type Edge,
  type ThemeColors,
} from './bounds'
import type { FragmentEditorState } from './useFragmentEditor'
import { shouldStopFragment, type PlayMode } from './playback'
import { PLAYBACK_VOLUME, WAVEFORM_HEIGHT_PX, waveformOf } from './waveform'

const props = defineProps<{
  editor: FragmentEditorState
  webApp: WebApp | undefined
}>()

const EDGES: { key: Edge; label: string }[] = [
  { key: 'start', label: 'Початок' },
  { key: 'end', label: 'Кінець' },
]
const STEPS = [
  { label: '−1 с', deltaMs: -COARSE_NUDGE_MS },
  { label: '−0,1 с', deltaMs: -FINE_NUDGE_MS },
  { label: '+0,1 с', deltaMs: FINE_NUDGE_MS },
  { label: '+1 с', deltaMs: COARSE_NUDGE_MS },
]
const FLAT_SENTENCE = 'Хвилю для цього аудіо не пораховано: орієнтуйтеся за часом.'

const waveformElement = ref<HTMLElement | null>(null)
const audioElement = ref<HTMLAudioElement | null>(null)
const surfer = shallowRef<WaveSurfer | null>(null)
const regionReady = ref(false)
const audioLoaded = ref(false)
const mode = ref<PlayMode>('idle')

let region: Region | null = null
let fragmentEndMs = 0
const leaveGuard = inject(LEAVE_GUARD, null)

let restoreSwipes: () => void = () => undefined

const waveform = computed(() =>
  props.editor.view.value === null ? null : waveformOf(props.editor.view.value),
)
const idle = computed(() => props.editor.status.value !== 'ready')
const notice = computed<Notice | null>(() =>
  props.editor.message.value === ''
    ? null
    : {
        kind: props.editor.messageKind.value,
        text: props.editor.message.value,
      },
)
const lengthMs = computed(() => props.editor.bounds.value.endMs - props.editor.bounds.value.startMs)

function edgeTime(edge: Edge): string {
  const { startMs, endMs } = props.editor.bounds.value
  return formatTime(edge === 'start' ? startMs : endMs)
}

function boundsOf(target: Region): Bounds {
  return { startMs: Math.round(target.start * 1000), endMs: Math.round(target.end * 1000) }
}

function currentColors(): ThemeColors {
  const style = getComputedStyle(document.documentElement)
  return themeColorsOf((name) => style.getPropertyValue(name))
}

function addRegion(plugin: RegionsPlugin, color: string): void {
  const limits = props.editor.limits.value
  if (limits === null || plugin.getRegions().length > 0) return
  const { startMs, endMs } = props.editor.bounds.value
  region = plugin.addRegion({
    ...regionStateOf({ startMs, endMs }, props.editor.status.value === 'ready'),
    color,
    minLength: SNAP_MS / 1000,
    maxLength: limits.maxLengthMs / 1000,
  })
  regionReady.value = true
}

function mount(): void {
  unmount()
  const view = props.editor.view.value
  const data = waveform.value
  const container = waveformElement.value
  const media = audioElement.value
  if (view === null || data === null || container === null || media === null) return

  const colors = currentColors()
  const regions = RegionsPlugin.create()
  const instance = WaveSurfer.create({
    container,
    media,
    url: props.editor.audioUrl.value,
    peaks: [data.channel],
    duration: view.audio_duration_ms / 1000,
    height: WAVEFORM_HEIGHT_PX,
    waveColor: colors.wave,
    progressColor: colors.progress,
    cursorColor: colors.progress,
    barWidth: 2,
    barGap: 1,
    barRadius: 2,
    plugins: [regions],
  })
  surfer.value = markRaw(instance)

  instance.on('ready', () => addRegion(regions, colors.region))
  instance.setVolume(PLAYBACK_VOLUME)
  instance.on('pause', () => (mode.value = 'idle'))
  instance.on('finish', () => (mode.value = 'idle'))
  // a tap on the waveform seeks, and a seek ends the fragment-only play
  instance.on('interaction', () => {
    if (mode.value === 'fragment') mode.value = 'track'
  })
  instance.on('timeupdate', (seconds) => {
    if (shouldStopFragment(mode.value, fragmentEndMs, seconds)) instance.pause()
  })
  instance.on('error', () => void recoverMedia())
  regions.on('region-update', (changed, side) =>
    props.editor.moveTo(boundsOf(changed), side ?? 'both', false),
  )
  regions.on('region-updated', (changed, side) =>
    props.editor.moveTo(boundsOf(changed), side ?? 'both', true),
  )
}

function unmount(): void {
  audioElement.value?.pause()
  surfer.value?.destroy()
  surfer.value = null
  region = null
  regionReady.value = false
  audioLoaded.value = false
  mode.value = 'idle'
}

// one fresh signed URL is fetched; a second failure leaves the sentence the editor has set
async function recoverMedia(): Promise<void> {
  if (!(await props.editor.refreshMedia())) return
  const instance = surfer.value
  const view = props.editor.view.value
  const data = waveform.value
  if (instance === null || view === null || data === null) return
  await instance
    .load(props.editor.audioUrl.value, [data.channel], view.audio_duration_ms / 1000)
    .catch(() => undefined)
  instance.setVolume(PLAYBACK_VOLUME)
}

function playFragment(): void {
  const instance = surfer.value
  if (instance === null) return
  if (mode.value === 'fragment') {
    instance.pause()
    return
  }
  const { startMs, endMs } = props.editor.bounds.value
  fragmentEndMs = endMs
  mode.value = 'fragment'
  instance.play(startMs / 1000).catch(() => (mode.value = 'idle'))
}

function playTrack(): void {
  const instance = surfer.value
  if (instance === null) return
  if (mode.value === 'track') {
    instance.pause()
    return
  }
  mode.value = 'track'
  instance.play().catch(() => (mode.value = 'idle'))
}

function setEdgeHere(edge: Edge): void {
  const instance = surfer.value
  if (instance !== null) props.editor.setEdgeAt(edge, instance.getCurrentTime())
}

function applyTheme(): void {
  const colors = currentColors()
  surfer.value?.setOptions({
    waveColor: colors.wave,
    progressColor: colors.progress,
    cursorColor: colors.progress,
  })
  region?.setOptions({ color: colors.region })
}

watch(
  () => props.editor.view.value,
  (view) => (view === null ? unmount() : mount()),
  { flush: 'post' },
)

// the region shows the bounds and is touchable only while ready, so a drag during a submit is
// undone when the editor returns to ready
function syncRegion(): void {
  if (region === null) return
  const next = props.editor.bounds.value
  const wanted = regionStateOf(next, props.editor.status.value === 'ready')
  const shown = boundsOf(region)
  const same = shown.startMs === next.startMs && shown.endMs === next.endMs
  if (same && region.drag === wanted.drag && region.resize === wanted.resize) return
  region.setOptions(wanted)
}

watch([() => props.editor.bounds.value, () => props.editor.status.value], syncRegion)

watch(
  () => props.editor.dirty.value,
  (dirty) => {
    // with a guard the closing confirmation follows all sources together
    if (leaveGuard === null) setClosingConfirmation(props.webApp, dirty)
    else leaveGuard.setDirty('fragment', dirty)
  },
  { immediate: true },
)

onMounted(() => {
  restoreSwipes = lockVerticalSwipes(props.webApp)
  props.webApp?.onEvent('themeChanged', applyTheme)
  if (props.editor.view.value === null) void props.editor.load()
  else mount()
})

onBeforeUnmount(() => {
  if (leaveGuard === null) setClosingConfirmation(props.webApp, false)
  else leaveGuard.setDirty('fragment', false)
  restoreSwipes()
  props.webApp?.offEvent('themeChanged', applyTheme)
  unmount()
})
</script>

<template>
  <div class="fragment">
    <p v-if="editor.view.value === null && editor.status.value === 'loading'" class="muted">
      Завантаження…
    </p>
    <template v-else-if="editor.view.value === null">
      <p class="error" role="alert">{{ editor.message.value }}</p>
      <button type="button" class="btn" @click="editor.load()">Спробувати знову</button>
    </template>
    <template v-else>
      <div ref="waveformElement" class="fragment__wave"></div>
      <audio
        ref="audioElement"
        preload="metadata"
        @loadedmetadata="audioLoaded = true"
        @canplay="editor.mediaPlayable()"
      ></audio>
      <p v-if="waveform?.flat" class="muted">{{ FLAT_SENTENCE }}</p>

      <p class="fragment__summary">
        Довжина: {{ formatTime(lengthMs) }}
        <span class="muted">
          (не більше {{ formatTime(editor.limits.value?.maxLengthMs ?? 0) }} із
          {{ formatTime(editor.limits.value?.durationMs ?? 0) }})
        </span>
      </p>

      <div class="fragment__players">
        <button
          type="button"
          class="btn fragment__play"
          :disabled="!regionReady"
          @click="playFragment()"
        >
          {{ mode === 'fragment' ? 'Зупинити' : 'Програти фрагмент' }}
        </button>
        <button
          type="button"
          class="btn btn--quiet fragment__track"
          :disabled="!regionReady"
          @click="playTrack()"
        >
          {{ mode === 'track' ? 'Пауза' : 'Грати трек' }}
        </button>
      </div>

      <div v-for="edge in EDGES" :key="edge.key" class="fragment__edge">
        <span class="fragment__time">{{ edge.label }}: {{ edgeTime(edge.key) }}</span>
        <span class="fragment__steps">
          <button
            type="button"
            class="btn btn--small fragment__here"
            :disabled="idle || !audioLoaded"
            @click="setEdgeHere(edge.key)"
          >
            {{ edge.key === 'start' ? 'Початок тут' : 'Кінець тут' }}
          </button>
          <button
            v-for="step in STEPS"
            :key="step.label"
            type="button"
            class="btn btn--small"
            :disabled="idle"
            @click="editor.nudgeEdge(edge.key, step.deltaMs)"
          >
            {{ step.label }}
          </button>
        </span>
      </div>

      <NoticeBanner :notice="notice" />

      <button
        v-if="editor.status.value !== 'submitted'"
        type="button"
        class="btn btn--quiet fragment__save"
        :disabled="!editor.canSave.value"
        @click="editor.save()"
      >
        {{ editor.status.value === 'saving' ? 'Збереження…' : 'Зберегти фрагмент' }}
      </button>
      <button
        v-if="editor.status.value !== 'submitted'"
        type="button"
        class="btn fragment__submit"
        :disabled="!editor.canSubmit.value"
        @click="editor.submit()"
      >
        {{ editor.status.value === 'submitting' ? 'Запуск…' : 'Зробити відео з цим фрагментом' }}
      </button>
      <button
        v-if="editor.status.value === 'failed'"
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
.fragment__wave {
  margin: 8px 0;
  touch-action: none;
}

.fragment__wave > div::part(region-handle) {
  width: 16px !important;
}

.fragment__summary {
  margin: 8px 0;
  font-variant-numeric: tabular-nums;
}

.fragment__edge {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin: 10px 0;
}

.fragment__time {
  font-variant-numeric: tabular-nums;
}

.fragment__steps {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.fragment__players {
  display: flex;
  gap: 8px;
}

.fragment__save,
.fragment__submit {
  width: 100%;
  margin-top: 6px;
}
</style>
