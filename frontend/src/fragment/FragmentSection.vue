<script setup lang="ts">
import { defineAsyncComponent, ref } from 'vue'
import type { ApiClient } from '../api/client'
import type { Schemas } from '../api/outcome'
import type { WebApp } from '../telegram/useTelegram'
import { formatTime } from './bounds'
import { useFragmentEditor } from './useFragmentEditor'
import { CHUNK_FAILED_MESSAGE, useLoadRetry } from './useLoadRetry'

const NOT_CHOOSABLE_FALLBACK = 'Фрагмент зараз обрати не можна.'

const props = defineProps<{
  fragment: Schemas['FragmentAvailability']
  client: ApiClient
  postId: number
  webApp: WebApp | undefined
}>()
const emit = defineEmits<{ submitted: [] }>()

const opened = ref(false)
const editor = useFragmentEditor(props.client, props.postId, () => emit('submitted'))
const chunk = useLoadRetry()

// the editor and wavesurfer load only when the admin opens it, so the post editor stays light
const FragmentEditor = defineAsyncComponent({
  loader: () => import('./FragmentEditor.vue'),
  onError: chunk.onError,
})

function currentRange(): string | null {
  const { start_ms: start, end_ms: end } = props.fragment
  return start === null || end === null ? null : `${formatTime(start)} – ${formatTime(end)}`
}
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
    <p v-else class="muted">{{ fragment.reason ?? NOT_CHOOSABLE_FALLBACK }}</p>
  </section>
</template>

<style>
.fragment-section__title {
  margin: 0 0 8px;
  font-size: 16px;
}
</style>
