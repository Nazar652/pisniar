<script setup lang="ts">
import { defineAsyncComponent, inject, ref, watch } from 'vue'
import type { ApiClient } from '../api/client'
import { LEAVE_GUARD, confirmDiscardOthers } from '../app/leaveGuard'
import { useLoadRetry } from '../fragment/useLoadRetry'
import { confirmWith } from '../telegram/actions'
import type { WebApp } from '../telegram/useTelegram'
import { useLyricsEditor } from './useLyricsEditor'

const CHUNK_FAILED_MESSAGE =
  'Не вдалося завантажити редактор тексту. Перевірте інтернет і спробуйте знову.'

const props = defineProps<{
  client: ApiClient
  postId: number
  webApp: WebApp | undefined
  fragmentRevision: number
}>()
const emit = defineEmits<{ saved: [] }>()

const opened = ref(false)
const leaveGuard = inject(LEAVE_GUARD, null)
const confirm = confirmWith(props.webApp)
const editor = useLyricsEditor(props.client, props.postId, {
  confirm,
  onSaved: () => emit('saved'),
  beforeRender: () => confirmDiscardOthers(leaveGuard, 'lyrics', confirm),
})
const chunk = useLoadRetry()

// the fragment editor saved other bounds: the open editor takes them, its text and audio stay
watch(
  () => props.fragmentRevision,
  () => void editor.refreshFragment(),
)

// the editor loads only when the admin opens it, so the post editor stays light
const LyricsEditor = defineAsyncComponent({
  loader: () => import('./LyricsEditor.vue'),
  onError: chunk.onError,
})
</script>

<template>
  <section class="card lyrics-section">
    <h2 class="lyrics-section__title">Текст</h2>
    <template v-if="opened">
      <template v-if="chunk.failed.value">
        <p class="error" role="alert">{{ CHUNK_FAILED_MESSAGE }}</p>
        <button type="button" class="btn" @click="chunk.retry()">Спробувати знову</button>
      </template>
      <LyricsEditor v-else :editor="editor" :web-app="webApp" />
    </template>
    <template v-else>
      <p class="muted">Текст для відео: рядки з часом, які видно у відео.</p>
      <button type="button" class="btn" @click="opened = true">Редагувати текст</button>
    </template>
  </section>
</template>

<style>
.lyrics-section__title {
  margin: 0 0 8px;
  font-size: 16px;
}
</style>
