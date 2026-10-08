<script setup lang="ts">
import type { Notice } from '../editor/usePostEditor'

defineProps<{ notice: Notice | null }>()
</script>

<template>
  <p
    v-if="notice"
    :class="['card', 'notice', `notice--${notice.kind}`]"
    :role="notice.kind === 'error' ? 'alert' : 'status'"
  >
    {{ notice.text }}
  </p>
</template>

<style>
.notice--error {
  color: var(--page-danger);
}

.notice--success {
  color: var(--page-ok);
}
</style>
