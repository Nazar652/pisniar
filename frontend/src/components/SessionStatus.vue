<script setup lang="ts">
import type { SessionState } from '../session/useSession'

defineProps<{ state: SessionState }>()
</script>

<template>
  <main class="status">
    <p v-if="state.kind === 'loading'" class="status__text">Вхід…</p>
    <p v-else-if="state.kind === 'ready'" class="status__text status__text--ok">
      Ви увійшли як адмін ({{ state.userId }})
    </p>
    <p v-else class="status__text status__text--error" role="alert">{{ state.message }}</p>
  </main>
</template>

<style>
.status {
  display: flex;
  min-height: 100vh;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--tg-theme-bg-color, #fff);
  color: var(--tg-theme-text-color, #000);
  font-family: system-ui, sans-serif;
  text-align: center;
}

.status__text--error {
  color: var(--tg-theme-destructive-text-color, #c0392b);
}
</style>
