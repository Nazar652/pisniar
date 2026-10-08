<script setup lang="ts">
import { computed } from 'vue'
import { displaySegments, droppedNotes, lengthLabel, type CaptionView } from '../editor/caption'

const props = defineProps<{ view: CaptionView | null; message: string }>()

const segments = computed(() => (props.view === null ? [] : displaySegments(props.view.segments)))
const notes = computed(() => (props.view === null ? [] : droppedNotes(props.view)))
</script>

<template>
  <section class="card preview">
    <div class="row preview__head">
      <strong>Підпис</strong>
      <span
        v-if="view"
        :class="['muted', 'preview__count', { 'preview__count--over': view.over_limit }]"
      >
        {{ lengthLabel(view) }}
      </span>
    </div>
    <p v-if="message" class="error" role="alert">{{ message }}</p>
    <p v-if="view" class="preview__text">
      <template v-for="(segment, index) in segments" :key="index">
        <br v-if="segment.kind === 'break'" />
        <a
          v-else-if="segment.kind === 'link'"
          :href="segment.href"
          target="_blank"
          rel="noopener noreferrer"
          >{{ segment.text }}</a
        >
        <template v-else>{{ segment.text }}</template>
      </template>
    </p>
    <p v-else-if="!message" class="muted">Підпис з’явиться тут.</p>
    <p v-for="note in notes" :key="note" class="muted preview__note">{{ note }}</p>
  </section>
</template>

<style>
.preview__head {
  justify-content: space-between;
}

.preview__text {
  margin: 0;
  overflow-wrap: anywhere;
}

.preview__count--over {
  color: var(--page-danger);
  font-weight: 600;
}

.preview__text a {
  color: var(--page-link);
}
</style>
