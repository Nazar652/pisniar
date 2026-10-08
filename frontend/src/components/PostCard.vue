<script setup lang="ts">
import { formatDate, joinArtists } from '../posts/format'
import type { PostSummary } from '../posts/usePostList'

defineProps<{ post: PostSummary }>()
const emit = defineEmits<{ open: [id: number] }>()
</script>

<template>
  <article
    class="card post-card"
    :class="{ 'post-card--locked': !post.editable }"
    @click="emit('open', post.id)"
  >
    <div class="row post-card__head">
      <strong class="post-card__title">{{ post.title ?? 'Без назви' }}</strong>
      <span v-if="post.has_video" class="muted" title="Є відео">🎬</span>
    </div>
    <p class="post-card__artists">{{ joinArtists(post.artists) }}</p>
    <p class="muted post-card__meta">
      #{{ post.id }} · {{ post.status_label }} ·
      {{ formatDate(post.published_at ?? post.created_at) }}
    </p>
    <p v-if="!post.editable && post.not_editable_reason" class="muted post-card__reason">
      {{ post.not_editable_reason }}
    </p>
  </article>
</template>

<style>
.post-card {
  cursor: pointer;
}

.post-card--locked {
  opacity: 0.85;
}

.post-card__head {
  justify-content: space-between;
}

.post-card__artists,
.post-card__meta,
.post-card__reason {
  margin: 2px 0 0;
}
</style>
