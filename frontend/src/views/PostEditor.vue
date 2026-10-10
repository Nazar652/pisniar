<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watchEffect } from 'vue'
import type { ApiClient } from '../api/client'
import ArtistsEditor from '../components/ArtistsEditor.vue'
import CaptionPreview from '../components/CaptionPreview.vue'
import FieldBlock from '../components/FieldBlock.vue'
import GenresEditor from '../components/GenresEditor.vue'
import LinksEditor from '../components/LinksEditor.vue'
import ListEditor from '../components/ListEditor.vue'
import NoticeBanner from '../components/NoticeBanner.vue'
import { inputValue } from '../editor/dom'
import { FIELD_LABELS, releaseTypeOptions } from '../editor/form'
import FragmentSection from '../fragment/FragmentSection.vue'
import { usePostEditor } from '../editor/usePostEditor'
import { applyMainButton, confirmWith, hapticWith } from '../telegram/actions'
import type { WebApp } from '../telegram/useTelegram'

const props = defineProps<{
  client: ApiClient
  postId: number
  webApp: WebApp | undefined
}>()

const editor = usePostEditor(props.client, props.postId, {
  confirm: confirmWith(props.webApp),
  haptic: hapticWith(props.webApp),
})
const { form, detail, meta } = editor

const locked = computed(() => !editor.editable.value || editor.busy.value)
const releaseTypes = computed(() =>
  meta.value === null || form.value === null
    ? []
    : releaseTypeOptions(meta.value, form.value.releaseType),
)

function saveFromButton(): void {
  void editor.save()
}

onMounted(() => {
  void editor.load()
  props.webApp?.MainButton.onClick(saveFromButton)
})

onBeforeUnmount(() => {
  editor.dispose()
  props.webApp?.MainButton.offClick(saveFromButton)
  props.webApp?.MainButton.hide()
})

// the Telegram button follows the editor: shown while editing is possible, spinning while busy
watchEffect(() => {
  if (props.webApp === undefined) return
  applyMainButton(props.webApp.MainButton, {
    text: 'Зберегти',
    visible: editor.status.value === 'ready' && editor.editable.value,
    enabled: !editor.busy.value,
    busy: editor.busy.value,
  })
})
</script>

<template>
  <main class="page">
    <p v-if="editor.status.value === 'loading'" class="muted">Завантаження…</p>
    <template v-else-if="editor.status.value === 'failed'">
      <p class="error" role="alert">{{ editor.loadMessage.value }}</p>
      <button type="button" class="btn" @click="editor.load()">Спробувати знову</button>
    </template>
    <template v-else-if="detail && form && meta">
      <h1 class="editor-title">Пост #{{ detail.id }} · {{ detail.status_label }}</h1>
      <p v-if="!editor.editable.value && detail.not_editable_reason" class="card muted">
        {{ detail.not_editable_reason }}
      </p>
      <p v-if="detail.other_post_ids.length > 0" class="muted">
        Є ще пости цього треку ({{ detail.other_post_ids.length }}):
        {{ detail.other_post_ids.map((id) => `#${id}`).join(', ') }}. Зміни їх не торкаються.
      </p>
      <NoticeBanner :notice="editor.notice.value" />

      <FieldBlock
        label="Назва"
        :manual="detail.manual_fields.includes('title')"
        :reverting="editor.revert.value.includes('title')"
        :error="editor.errorOf('title')"
        @revert="editor.toggleRevert('title')"
      >
        <input
          class="input"
          :value="form.title"
          :disabled="locked"
          @input="form.title = inputValue($event)"
        />
      </FieldBlock>

      <FieldBlock
        label="Артисти"
        :manual="detail.manual_fields.includes('artists')"
        :reverting="editor.revert.value.includes('artists')"
        :error="editor.errorOf('artists')"
        @revert="editor.toggleRevert('artists')"
      >
        <ArtistsEditor v-model="form.artists" :disabled="locked" :error-of="editor.errorOf" />
      </FieldBlock>

      <FieldBlock label="Мови" :error="editor.errorOf('languages')">
        <ListEditor
          v-model="form.languages"
          :disabled="locked"
          :suggestions="meta.languages"
          list-id="language-suggestions"
          placeholder="Додати мову"
          field-name="languages"
          :error-of="editor.errorOf"
        />
      </FieldBlock>

      <FieldBlock label="Жанри" hint="Хештег за замовчуванням видно в полі.">
        <GenresEditor
          v-model="form.genres"
          :disabled="locked"
          :meta="meta"
          :error-of="editor.errorOf"
        />
      </FieldBlock>

      <FieldBlock
        :label="FIELD_LABELS['year'] ?? 'Рік'"
        :manual="detail.manual_fields.includes('year')"
        :reverting="editor.revert.value.includes('year')"
        :error="editor.errorOf('year')"
        @revert="editor.toggleRevert('year')"
      >
        <input
          class="input"
          inputmode="numeric"
          :value="form.year"
          :disabled="locked"
          @input="form.year = inputValue($event)"
        />
      </FieldBlock>

      <FieldBlock
        label="Тип релізу"
        :manual="detail.manual_fields.includes('release_type')"
        :reverting="editor.revert.value.includes('release_type')"
        :error="editor.errorOf('release_type')"
        @revert="editor.toggleRevert('release_type')"
      >
        <select
          class="input"
          :value="form.releaseType"
          :disabled="locked"
          @change="form.releaseType = inputValue($event)"
        >
          <option v-for="option in releaseTypes" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
      </FieldBlock>

      <FieldBlock
        label="Назва релізу"
        :manual="detail.manual_fields.includes('release_title')"
        :reverting="editor.revert.value.includes('release_title')"
        :error="editor.errorOf('release_title')"
        @revert="editor.toggleRevert('release_title')"
      >
        <input
          class="input"
          :value="form.releaseTitle"
          :disabled="locked"
          @input="form.releaseTitle = inputValue($event)"
        />
      </FieldBlock>

      <FieldBlock
        label="Лінк на альбом"
        hint="У підписі видно лише для типу «Альбом» з назвою релізу."
        :manual="detail.manual_fields.includes('album_id_spotify')"
        :reverting="editor.revert.value.includes('album_id_spotify')"
        :error="editor.errorOf('album_link')"
        @revert="editor.toggleRevert('album_id_spotify')"
      >
        <input
          class="input"
          type="url"
          :value="form.albumLink"
          :disabled="locked"
          placeholder="https://open.spotify.com/album/…"
          @input="form.albumLink = inputValue($event)"
        />
      </FieldBlock>

      <FieldBlock label="Лінки платформ" :error="editor.errorOf('revert_to_automatic')">
        <LinksEditor
          v-model="form.links"
          :disabled="locked"
          :meta="meta"
          :removed="detail.removed_platforms"
          :error-of="editor.errorOf"
        />
        <p v-if="editor.warning.value" class="error" role="alert">{{ editor.warning.value }}</p>
      </FieldBlock>

      <CaptionPreview :view="editor.preview.value" :message="editor.previewMessage.value" />

      <FragmentSection
        :fragment="detail.fragment"
        :client="client"
        :post-id="postId"
        :web-app="webApp"
        @submitted="editor.reloadIfClean()"
      />

      <div v-if="editor.canApplyToChannel.value" class="card">
        <p class="muted">Підпис у каналі відрізняється від збереженого.</p>
        <button
          type="button"
          class="btn"
          :disabled="editor.busy.value"
          @click="editor.applyToChannel()"
        >
          Застосувати в каналі
        </button>
      </div>

      <div v-if="editor.channelMissing.value" class="card">
        <p class="error">Допис у каналі зник.</p>
        <button
          type="button"
          class="btn btn--danger"
          :disabled="editor.busy.value"
          @click="editor.archive()"
        >
          Позначити як архівний
        </button>
      </div>
    </template>
  </main>
</template>

<style>
.editor-title {
  margin: 4px 0 12px;
  font-size: 20px;
}
</style>
