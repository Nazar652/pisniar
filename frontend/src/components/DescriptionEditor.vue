<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { inputValue } from '../editor/dom'
import { descriptionCounter, wrapAsLink } from '../editor/description'

const text = defineModel<string>({ required: true })
const props = defineProps<{
  disabled: boolean
  maxLength: number
  note: string | null
  hostsNote: string | null
  warnings: readonly string[]
}>()

const area = ref<HTMLTextAreaElement | null>(null)

async function addLink(): Promise<void> {
  const element = area.value
  const start = element?.selectionStart ?? text.value.length
  const end = element?.selectionEnd ?? start
  const edit = wrapAsLink(text.value, start, end)
  text.value = edit.text
  await nextTick()
  element?.focus()
  element?.setSelectionRange(edit.selectionStart, edit.selectionEnd)
}
</script>

<template>
  <div>
    <textarea
      ref="area"
      class="input description__area"
      rows="5"
      :value="text"
      :maxlength="props.maxLength"
      :disabled="disabled"
      @input="text = inputValue($event)"
    ></textarea>
    <div class="row description__bar">
      <button type="button" class="btn btn--quiet" :disabled="disabled" @click="addLink">
        Додати посилання
      </button>
      <span class="muted description__counter">{{
        descriptionCounter(text, props.maxLength)
      }}</span>
    </div>
    <p v-if="hostsNote" class="muted">{{ hostsNote }}</p>
    <p v-for="(warning, index) in warnings" :key="index" class="error">{{ warning }}</p>
    <p v-if="note" class="muted">{{ note }}</p>
  </div>
</template>

<style>
.description__area {
  resize: vertical;
}

.description__bar {
  justify-content: space-between;
}
</style>
