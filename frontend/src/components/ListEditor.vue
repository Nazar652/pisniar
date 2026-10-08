<script setup lang="ts">
import { ref } from 'vue'
import { inputValue } from '../editor/dom'
import { moveItem, removeAt } from '../editor/form'

const items = defineModel<string[]>({ required: true })
const props = defineProps<{
  disabled: boolean
  suggestions: readonly string[]
  listId: string
  placeholder: string
  errorOf: (field: string) => string | undefined
  fieldName: string
}>()

const draft = ref('')

function change(index: number, value: string): void {
  items.value = items.value.map((item, position) => (position === index ? value : item))
}

function add(): void {
  const value = draft.value.trim()
  if (value === '') return
  items.value = [...items.value, value]
  draft.value = ''
}
</script>

<template>
  <div>
    <div v-for="(item, index) in items" :key="index">
      <div class="row">
        <input
          class="input"
          :value="item"
          :disabled="disabled"
          @input="change(index, inputValue($event))"
        />
        <button
          type="button"
          class="btn btn--small"
          :disabled="disabled || index === 0"
          @click="items = moveItem(items, index, -1)"
        >
          ↑
        </button>
        <button
          type="button"
          class="btn btn--small"
          :disabled="disabled || index === items.length - 1"
          @click="items = moveItem(items, index, 1)"
        >
          ↓
        </button>
        <button
          type="button"
          class="btn btn--small btn--danger"
          :disabled="disabled"
          @click="items = removeAt(items, index)"
        >
          ✕
        </button>
      </div>
      <p v-if="props.errorOf(`${fieldName}[${index}]`)" class="error">
        {{ props.errorOf(`${fieldName}[${index}]`) }}
      </p>
    </div>
    <div class="row">
      <input
        v-model="draft"
        class="input"
        :list="listId"
        :disabled="disabled"
        :placeholder="placeholder"
        @keydown.enter.prevent="add"
      />
      <button type="button" class="btn btn--small" :disabled="disabled" @click="add">+</button>
    </div>
    <datalist :id="listId">
      <option v-for="suggestion in suggestions" :key="suggestion" :value="suggestion" />
    </datalist>
  </div>
</template>
