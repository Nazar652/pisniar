<script setup lang="ts">
import { inputValue } from '../editor/dom'
import { moveItem, removeAt, type ArtistRow } from '../editor/form'

const rows = defineModel<ArtistRow[]>({ required: true })
defineProps<{ disabled: boolean; errorOf: (field: string) => string | undefined }>()

function change(index: number, patch: Partial<ArtistRow>): void {
  rows.value = rows.value.map((row, position) => (position === index ? { ...row, ...patch } : row))
}

function add(): void {
  rows.value = [...rows.value, { name: '', hashtag: '', defaultHashtag: null }]
}
</script>

<template>
  <div>
    <p class="muted artist__format">
      Перший рядок поста: «Ім’я 1, Ім’я 2 — Назва треку». Хештеги артистів — у другому рядку.
    </p>
    <div v-for="(row, index) in rows" :key="index" class="artist">
      <p class="muted artist__label">Ім’я — у першому рядку поста</p>
      <div class="row">
        <input
          class="input"
          :value="row.name"
          :disabled="disabled"
          placeholder="Ім’я артиста"
          @input="change(index, { name: inputValue($event) })"
        />
        <button
          type="button"
          class="btn btn--small"
          :disabled="disabled || index === 0"
          @click="rows = moveItem(rows, index, -1)"
        >
          ↑
        </button>
        <button
          type="button"
          class="btn btn--small"
          :disabled="disabled || index === rows.length - 1"
          @click="rows = moveItem(rows, index, 1)"
        >
          ↓
        </button>
        <button
          type="button"
          class="btn btn--small btn--danger"
          :disabled="disabled || rows.length === 1"
          @click="rows = removeAt(rows, index)"
        >
          ✕
        </button>
      </div>
      <p class="muted artist__label">Хештег</p>
      <div class="row">
        <input
          class="input"
          :value="row.hashtag"
          :disabled="disabled"
          :placeholder="row.defaultHashtag ? `#${row.defaultHashtag}` : 'Хештег'"
          @input="change(index, { hashtag: inputValue($event) })"
        />
      </div>
      <p v-if="row.defaultHashtag" class="muted artist__hint">
        порожньо — буде #{{ row.defaultHashtag }}
      </p>
      <p v-if="errorOf(`artists[${index}].name`)" class="error">
        {{ errorOf(`artists[${index}].name`) }}
      </p>
      <p v-if="errorOf(`artists[${index}].hashtag`)" class="error">
        {{ errorOf(`artists[${index}].hashtag`) }}
      </p>
    </div>
    <button type="button" class="btn btn--quiet" :disabled="disabled" @click="add">+ Артист</button>
  </div>
</template>

<style>
.artist {
  margin-bottom: 12px;
}

.artist__label,
.artist__hint,
.artist__format {
  margin: 0 0 4px;
  font-size: 13px;
}
</style>
