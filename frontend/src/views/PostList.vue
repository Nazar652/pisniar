<script setup lang="ts">
import { onMounted } from 'vue'
import type { ApiClient } from '../api/client'
import PostCard from '../components/PostCard.vue'
import { usePostList } from '../posts/usePostList'

const props = defineProps<{ client: ApiClient }>()
const emit = defineEmits<{ open: [id: number] }>()

const list = usePostList(props.client)

onMounted(() => list.load())
</script>

<template>
  <main class="page">
    <h1 class="list-title">Пости</h1>
    <p v-if="list.status.value === 'loading' || list.status.value === 'idle'" class="muted">
      Завантаження…
    </p>
    <template v-else-if="list.status.value === 'failed'">
      <p class="error" role="alert">{{ list.message.value }}</p>
      <button type="button" class="btn" @click="list.load()">Спробувати знову</button>
    </template>
    <template v-else>
      <p v-if="list.items.value.length === 0" class="muted">Постів поки немає.</p>
      <PostCard
        v-for="post in list.items.value"
        :key="post.id"
        :post="post"
        @open="emit('open', $event)"
      />
      <p v-if="list.message.value" class="error" role="alert">{{ list.message.value }}</p>
      <button
        v-if="list.nextBeforeId.value !== null"
        type="button"
        class="btn"
        :disabled="list.loadingMore.value"
        @click="list.loadMore()"
      >
        {{ list.loadingMore.value ? 'Завантаження…' : 'Ще' }}
      </button>
    </template>
  </main>
</template>

<style>
.list-title {
  margin: 4px 0 12px;
  font-size: 22px;
}
</style>
