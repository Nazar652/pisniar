<script setup lang="ts">
import { inputValue } from '../editor/dom'
import { genreDefaultHashtag, removeAt, type GenreRow, type MetaResponse } from '../editor/form'

const rows = defineModel<GenreRow[]>({ required: true })
const props = defineProps<{
  disabled: boolean
  meta: MetaResponse
  errorOf: (field: string) => string | undefined
}>()

function change(index: number, patch: Partial<GenreRow>): void {
  rows.value = rows.value.map((row, position) => (position === index ? { ...row, ...patch } : row))
}

function rename(index: number, name: string): void {
  change(index, { name, defaultHashtag: genreDefaultHashtag(props.meta, name) })
}

function add(): void {
  rows.value = [...rows.value, { name: '', hashtag: '', defaultHashtag: null }]
}
</script>

<template>
  <div>
    <div v-for="(row, index) in rows" :key="index">
      <div class="row">
        <input
          class="input"
          list="genre-suggestions"
          :value="row.name"
          :disabled="disabled"
          placeholder="Жанр"
          @input="rename(index, inputValue($event))"
        />
        <button
          type="button"
          class="btn btn--small btn--danger"
          :disabled="disabled"
          @click="rows = removeAt(rows, index)"
        >
          ✕
        </button>
      </div>
      <div class="row">
        <input
          class="input"
          :value="row.hashtag"
          :disabled="disabled"
          :placeholder="row.defaultHashtag ? `#${row.defaultHashtag}` : 'Хештег'"
          @input="change(index, { hashtag: inputValue($event) })"
        />
      </div>
      <p v-if="props.errorOf(`genres[${index}].name`)" class="error">
        {{ props.errorOf(`genres[${index}].name`) }}
      </p>
      <p v-if="props.errorOf(`genres[${index}].hashtag`)" class="error">
        {{ props.errorOf(`genres[${index}].hashtag`) }}
      </p>
    </div>
    <button type="button" class="btn btn--quiet" :disabled="disabled" @click="add">+ Жанр</button>
    <datalist id="genre-suggestions">
      <option v-for="genre in meta.genres" :key="genre.name" :value="genre.name" />
    </datalist>
  </div>
</template>
