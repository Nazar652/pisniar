<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import type { ApiClient } from '../api/client'
import type { Schemas } from '../api/outcome'
import type { WebApp } from '../telegram/useTelegram'
import { formatTime } from './bounds'
import { useAudioPreparation } from './useAudioPreparation'
import { useFragmentEditor } from './useFragmentEditor'
import { CHUNK_FAILED_MESSAGE, useLoadRetry } from './useLoadRetry'

const NOT_CHOOSABLE_FALLBACK = 'Фрагмент зараз обрати не можна.'
const PREPARING_LINE = 'Готуємо аудіо…'

const props = defineProps<{
  fragment: Schemas['FragmentAvailability']
  client: ApiClient
  postId: number
  webApp: WebApp | undefined
}>()
const emit = defineEmits<{ submitted: []; prepared: [] }>()

const opened = ref(false)
const editor = useFragmentEditor(props.client, props.postId, () => emit('submitted'))
const preparation = useAudioPreparation(props.client, props.postId, () => {
  opened.value = true
  emit('prepared')
})
const chunk = useLoadRetry()

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
        Зараз у відео: {{ currentRange() }}
      </p>
      <template v-if="opened">
        <template v-if="chunk.failed.value">
          <p class="error" role="alert">{{ CHUNK_FAILED_MESSAGE }}</p>
          <button type="button" class="btn" @click="chunk.retry()">Спробувати знову</button>
        </template>
        <FragmentEditor v-else :editor="editor" :web-app="webApp" />
      </template>
      <button v-else type="button" class="btn" @click="opened = true">Обрати фрагмент</button>
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
  </section>
</template>

<style>
.fragment-section__title {
  margin: 0 0 8px;
  font-size: 16px;
}
</style>
