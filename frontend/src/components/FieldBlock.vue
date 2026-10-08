<script setup lang="ts">
defineProps<{
  label: string
  manual?: boolean
  reverting?: boolean
  error?: string | undefined
  hint?: string
}>()

const emit = defineEmits<{ revert: [] }>()
</script>

<template>
  <section class="card field">
    <div class="row field__head">
      <strong class="field__label">{{ label }}</strong>
      <span v-if="manual" class="muted field__badge">змінено вручну</span>
      <button
        v-if="manual"
        type="button"
        class="btn btn--quiet field__revert"
        @click="emit('revert')"
      >
        {{ reverting ? 'Лишити ручним' : 'Повернути автоматичне' }}
      </button>
    </div>
    <slot />
    <p v-if="hint" class="muted field__hint">{{ hint }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>

<style>
.field__head {
  flex-wrap: wrap;
  justify-content: space-between;
}

.field__revert {
  padding: 0;
}
</style>
