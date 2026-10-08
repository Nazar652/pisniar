<script setup lang="ts">
import { computed, ref } from 'vue'
import { inputValue } from '../editor/dom'
import {
  platformLabel,
  platformsToAdd,
  removeAt,
  type LinkRow,
  type MetaResponse,
} from '../editor/form'

const links = defineModel<LinkRow[]>({ required: true })
const props = defineProps<{
  disabled: boolean
  meta: MetaResponse
  removed: readonly string[]
  errorOf: (field: string) => string | undefined
}>()

const chosen = ref('')
const available = computed(() =>
  platformsToAdd(
    props.meta,
    links.value.map((row) => row.platform),
    props.removed,
  ),
)

function change(index: number, url: string): void {
  links.value = links.value.map((row, position) => (position === index ? { ...row, url } : row))
}

function add(): void {
  const platform = chosen.value || available.value[0]
  if (platform === undefined) return
  links.value = [...links.value, { platform, url: '' }]
  chosen.value = ''
}
</script>

<template>
  <div>
    <div v-for="(row, index) in links" :key="row.platform">
      <p class="muted">{{ platformLabel(meta, row.platform) }}</p>
      <div class="row">
        <input
          class="input"
          type="url"
          :value="row.url"
          :disabled="disabled"
          placeholder="https://…"
          @input="change(index, inputValue($event))"
        />
        <button
          type="button"
          class="btn btn--small btn--danger"
          :disabled="disabled"
          @click="links = removeAt(links, index)"
        >
          ✕
        </button>
      </div>
      <p v-if="props.errorOf(`platform_links.${row.platform}`)" class="error">
        {{ props.errorOf(`platform_links.${row.platform}`) }}
      </p>
    </div>
    <div v-if="available.length > 0" class="row">
      <select v-model="chosen" class="input" :disabled="disabled">
        <option value="">Додати платформу…</option>
        <option v-for="platform in available" :key="platform" :value="platform">
          {{ platformLabel(meta, platform) }}{{ removed.includes(platform) ? ' (прибрано)' : '' }}
        </option>
      </select>
      <button type="button" class="btn btn--small" :disabled="disabled" @click="add">+</button>
    </div>
  </div>
</template>
