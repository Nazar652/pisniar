<script setup lang="ts">
import { computed, defineAsyncComponent, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ApiClient } from '../api/client'
import type { Schemas } from '../api/outcome'
import { LEAVE_GUARD, confirmDiscardOthers } from '../app/leaveGuard'
import NoticeBanner from '../components/NoticeBanner.vue'
import type { Notice } from '../editor/usePostEditor'
import { confirmWith } from '../telegram/actions'
import type { WebApp } from '../telegram/useTelegram'
import { formatTime } from './bounds'
import { useAudioPreparation } from './useAudioPreparation'
import { useCancelPreparation } from './useCancelPreparation'
import { useFragmentEditor, type FragmentSaved } from './useFragmentEditor'
import { CHUNK_FAILED_MESSAGE, useLoadRetry } from './useLoadRetry'

const NOT_CHOOSABLE_FALLBACK = 'Фрагмент зараз обрати не можна.'
const PREPARING_LINE = 'Готуємо аудіо…'
const STALE_PREVIEW_NOTICE: Notice = {
  kind: 'info',
  text: 'Фрагмент змінено — прев’ю застаріло, зробіть відео заново.',
}

const props = defineProps<{
  fragment: Schemas['FragmentAvailability']
  client: ApiClient
  postId: number
  postStamp: string
  webApp: WebApp | undefined
}>()
const emit = defineEmits<{
  submitted: []
  saved: [saved: FragmentSaved, sentStamp: string]
  prepared: []
  cancelled: []
}>()

const opened = ref(false)
const leaveGuard = inject(LEAVE_GUARD, null)
const confirm = confirmWith(props.webApp)
const keepOthers = () => confirmDiscardOthers(leaveGuard, 'fragment', confirm)
const editor = useFragmentEditor(
  props.client,
  props.postId,
  () => emit('submitted'),
  keepOthers,
  (saved, sentStamp) => emit('saved', saved, sentStamp),
)
const preparation = useAudioPreparation(props.client, props.postId, () => {
  opened.value = true
  emit('prepared')
})
const cancellation = useCancelPreparation(
  props.client,
  props.postId,
  () => props.postStamp,
  (cancelled) => {
    if (cancelled) opened.value = false
    emit('cancelled')
  },
  keepOthers,
)
const chunk = useLoadRetry()

watch(
  () => props.postStamp,
  (stamp) => editor.adoptStamp(stamp),
)

// the editor and wavesurfer load only when the admin opens it, so the post editor stays light
const FragmentEditor = defineAsyncComponent({
  loader: () => import('./FragmentEditor.vue'),
  onError: chunk.onError,
})

const waiting = computed(
  () =>
    preparation.status.value === 'requesting' ||
    preparation.status.value === 'preparing' ||
    (preparation.status.value === 'idle' && props.fragment.preparing),
)
const stopped = computed(
  () => preparation.status.value === 'failed' || preparation.status.value === 'timed_out',
)
const canPrepare = computed(
  () =>
    props.fragment.preparable || props.fragment.preparing || preparation.status.value !== 'idle',
)

function currentRange(): string | null {
  const { start_ms: start, end_ms: end } = props.fragment
  return start === null || end === null ? null : `${formatTime(start)} – ${formatTime(end)}`
}

onMounted(() => {
  if (props.fragment.preparing) preparation.resume()
})

// a download started elsewhere is seen after a reload of the post, not only at mount
preparation.followPreparing(() => props.fragment.preparing)

onBeforeUnmount(() => preparation.dispose())
</script>

<template>
  <section class="card fragment-section">
    <h2 class="fragment-section__title">Фрагмент</h2>
    <template v-if="fragment.choosable || opened">
      <p v-if="fragment.choosable && currentRange()" class="muted">
        {{ fragment.preview_stale ? 'Обраний фрагмент' : 'Зараз у відео' }}: {{ currentRange() }}
      </p>
      <NoticeBanner :notice="fragment.preview_stale ? STALE_PREVIEW_NOTICE : null" />
      <template v-if="opened">
        <template v-if="chunk.failed.value">
          <p class="error" role="alert">{{ CHUNK_FAILED_MESSAGE }}</p>
          <button type="button" class="btn" @click="chunk.retry()">Спробувати знову</button>
        </template>
        <FragmentEditor v-else :editor="editor" :web-app="webApp" />
      </template>
      <button v-else type="button" class="btn" @click="opened = true">Обрати фрагмент</button>
      <button
        v-if="fragment.cancellable"
        type="button"
        class="btn btn--quiet"
        :disabled="cancellation.busy.value"
        @click="cancellation.cancel()"
      >
        Скасувати підготовку
      </button>
    </template>
    <template v-else-if="canPrepare">
      <p v-if="waiting" class="muted" role="status">{{ PREPARING_LINE }}</p>
      <template v-else-if="stopped">
        <p class="error" role="alert">{{ preparation.message.value }}</p>
        <button
          v-if="preparation.status.value === 'timed_out'"
          type="button"
          class="btn"
          @click="preparation.resume()"
        >
          Перевірити знову
        </button>
        <button v-else type="button" class="btn" @click="preparation.start()">
          Спробувати знову
        </button>
      </template>
      <p v-else-if="preparation.status.value === 'unavailable'" class="muted">
        {{ preparation.message.value }}
      </p>
      <template v-else>
        <p class="muted">{{ fragment.reason ?? NOT_CHOOSABLE_FALLBACK }}</p>
        <button type="button" class="btn" @click="preparation.start()">Обрати фрагмент</button>
      </template>
    </template>
    <p v-else class="muted">{{ fragment.reason ?? NOT_CHOOSABLE_FALLBACK }}</p>
    <p
      v-if="cancellation.message.value"
      :class="cancellation.failed.value ? 'error' : 'muted'"
      :role="cancellation.failed.value ? 'alert' : 'status'"
    >
      {{ cancellation.message.value }}
    </p>
  </section>
</template>

<style>
.fragment-section__title {
  margin: 0 0 8px;
  font-size: 16px;
}
</style>
