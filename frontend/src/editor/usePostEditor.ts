import { computed, ref, shallowRef, watch } from 'vue'
import type { ApiClient } from '../api/client'
import { outcomeOf, type Failure, type Schemas } from '../api/outcome'
import type { Confirm, Haptic } from '../telegram/actions'
import {
  effectiveRevert,
  formFromDetail,
  isDirty,
  toFieldsInput,
  toUpdate,
  yearProblem,
  youtubeMusicWarning,
  type FormState,
  type MetaResponse,
  type PostDetail,
} from './form'
import type { CaptionView } from './caption'
import { confirmWithHosts, type DescriptionDestination } from './description'

export const PREVIEW_DEBOUNCE_MS = 400
export const CONFIRM_APPLY_TEXT =
  'Змінити підпис опублікованого допису в каналі? Підписники побачать нову версію.'
export const CONFIRM_ARCHIVE_TEXT =
  'Повідомлення в каналі зникло. Позначити пост архівним? Повернути його буде не можна.'
export const CONFIRM_COVER_TEXT =
  'Пост піде в канал лише з обкладинкою і підписом, без відео. Скасувати публікацію не можна. Опублікувати?'
export const COVER_PUBLISHING_MESSAGE = 'Публікую без відео. Підтвердження прийде в чат.'
export const INVALID_YEAR_MESSAGE = 'Деякі поля заповнено неправильно.'
export const NOTHING_TO_APPLY_MESSAGE = 'У посту ще немає підпису: спершу збережіть поля.'

const ROLE_NAMES: Record<string, string> = {
  draft: 'чернетку в чаті',
  preview: 'прев’ю відео в чаті',
  channel: 'допис у каналі',
}
const APPLY_RESULTS: Record<string, string> = {
  edited: 'Збережено й застосовано.',
  not_modified: 'Збережено. Повідомлення вже мало цей підпис.',
  stored_only: 'Збережено. Повідомлень для оновлення немає.',
}

type FragmentSaved = Schemas['FragmentSaved']
export type EditorStatus = 'loading' | 'ready' | 'failed'
export interface Notice {
  kind: 'success' | 'error' | 'info'
  text: string
}
export interface EditorDeps {
  confirm: Confirm
  haptic: Haptic
  debounceMs?: number
}

// a field with several problems (the links of the description) shows all of them
function fieldErrorMap(failure: Failure): Record<string, string> {
  const messages: Record<string, string> = {}
  for (const error of failure.fieldErrors) {
    const earlier = messages[error.field]
    messages[error.field] = earlier === undefined ? error.message : `${earlier} ${error.message}`
  }
  return messages
}

export function usePostEditor(client: ApiClient, postId: number, deps: EditorDeps) {
  const status = ref<EditorStatus>('loading')
  const loadMessage = ref('')
  const detail = shallowRef<PostDetail | null>(null)
  const meta = shallowRef<MetaResponse | null>(null)
  const form = ref<FormState | null>(null)
  const revert = ref<string[]>([])
  const fieldErrors = ref<Record<string, string>>({})
  const notice = ref<Notice | null>(null)
  const preview = shallowRef<CaptionView | null>(null)
  const descriptionLinks = shallowRef<DescriptionDestination[]>([])
  const descriptionOverLimit = ref(false)
  const previewMessage = ref('')
  const busy = ref(false)
  const channelMissing = ref(false)
  const archived = ref(false)

  let previewTimer: ReturnType<typeof setTimeout> | undefined
  let previewSequence = 0

  const isPublished = computed(() => detail.value?.published_at != null)
  const editable = computed(() => detail.value?.editable === true && !archived.value)
  const dirty = computed(() =>
    detail.value === null || form.value === null ? false : isDirty(form.value, detail.value),
  )
  const canApplyToChannel = computed(
    () => isPublished.value && editable.value && detail.value?.stored_caption_matches === false,
  )
  const coverAllowed = computed(
    () => detail.value?.cover_publication.allowed === true && !archived.value,
  )
  const canPublishCover = computed(
    () => coverAllowed.value && detail.value?.stored_caption_matches === true && !dirty.value,
  )
  const warning = computed(() =>
    detail.value === null || form.value === null
      ? null
      : youtubeMusicWarning(form.value, detail.value),
  )
  const revertNames = computed(() =>
    detail.value === null || form.value === null
      ? []
      : effectiveRevert(revert.value, form.value, detail.value),
  )

  // a confirmation names the hosts of the saved description, not of what is being typed
  function storedLinks(): DescriptionDestination[] {
    return detail.value?.description_links ?? []
  }

  function adopt(next: PostDetail): void {
    detail.value = next
    form.value = formFromDetail(next)
    revert.value = []
    fieldErrors.value = {}
    preview.value = null
    descriptionLinks.value = []
    descriptionOverLimit.value = false
    previewMessage.value = ''
  }

  // a quiet reload keeps the form on screen while the fresh data is fetched
  async function load(quiet = false): Promise<void> {
    if (!quiet) status.value = 'loading'
    const [loaded, metaLoaded] = await Promise.all([
      outcomeOf(client.GET('/api/posts/{post_id}', { params: { path: { post_id: postId } } })),
      meta.value === null ? outcomeOf(client.GET('/api/meta')) : Promise.resolve(null),
    ])
    if (!loaded.ok) {
      if (quiet) {
        say('error', loaded.failure.message)
        return
      }
      status.value = 'failed'
      loadMessage.value = loaded.failure.message
      return
    }
    if (metaLoaded !== null) {
      if (!metaLoaded.ok) {
        status.value = 'failed'
        loadMessage.value = metaLoaded.failure.message
        return
      }
      meta.value = metaLoaded.data
    }
    adopt(loaded.data)
    status.value = 'ready'
    schedulePreview()
  }

  // a reload replaces the form, so it never runs over unsaved edits
  async function reloadIfClean(): Promise<boolean> {
    if (dirty.value || busy.value) return false
    await load(true)
    return true
  }

  // a fragment save with unsaved field edits on screen cannot reload: only the bounds, the verdict and the stamp
  // move, and only while the post is still the version the save was sent against
  function adoptFragmentSave(saved: FragmentSaved, sentStamp: string): void {
    const current = detail.value
    if (current === null || current.expected_post_updated_at !== sentStamp) return
    detail.value = {
      ...current,
      expected_post_updated_at: saved.post_updated_at,
      fragment: {
        ...current.fragment,
        start_ms: saved.start_ms,
        end_ms: saved.end_ms,
        preview_stale: saved.preview_stale,
      },
    }
  }

  function schedulePreview(): void {
    clearTimeout(previewTimer)
    previewTimer = setTimeout(() => void refreshPreview(), deps.debounceMs ?? PREVIEW_DEBOUNCE_MS)
  }

  // an answer that is not the latest request's is dropped, so a slow one cannot overwrite a newer
  async function refreshPreview(): Promise<void> {
    const current = form.value
    if (current === null || !editable.value || yearProblem(current.year) !== null) return
    previewSequence += 1
    const sequence = previewSequence
    const answer = await outcomeOf(
      client.POST('/api/posts/{post_id}/preview', {
        params: { path: { post_id: postId } },
        body: toFieldsInput(current, []),
      }),
    )
    if (sequence !== previewSequence) return
    if (answer.ok) {
      preview.value = answer.data.caption
      descriptionLinks.value = answer.data.description_links
      descriptionOverLimit.value = answer.data.description_over_limit
      previewMessage.value = ''
      fieldErrors.value = {}
      return
    }
    previewMessage.value = answer.failure.message
    if (answer.failure.code === 'invalid_fields') fieldErrors.value = fieldErrorMap(answer.failure)
  }

  watch(form, schedulePreview, { deep: true })

  function say(kind: Notice['kind'], text: string): void {
    notice.value = { kind, text }
    if (kind !== 'info') deps.haptic(kind)
  }

  async function reloadAfter(failure: Failure): Promise<void> {
    await load(true)
    say('error', failure.message)
  }

  async function fail(failure: Failure): Promise<void> {
    if (failure.code === 'invalid_fields') {
      fieldErrors.value = fieldErrorMap(failure)
      say('error', failure.message)
    } else if (
      failure.code === 'stale_data' ||
      failure.code === 'caption_changed' ||
      failure.code === 'not_publishable'
    ) {
      await reloadAfter(failure)
    } else if (failure.code === 'channel_message_missing') {
      channelMissing.value = true
      say('error', failure.message)
    } else {
      say('error', failure.message)
    }
  }

  async function applyCaption(sha256: string): Promise<boolean> {
    const answer = await outcomeOf(
      client.POST('/api/posts/{post_id}/apply', {
        params: { path: { post_id: postId } },
        body: { expected_caption_sha256: sha256 },
      }),
    )
    if (!answer.ok) {
      await fail(answer.failure)
      return false
    }
    const failedRoles = answer.data.other_messages_failed.map((role) => ROLE_NAMES[role] ?? role)
    const base = APPLY_RESULTS[answer.data.result] ?? 'Готово.'
    const extra = failedRoles.length === 0 ? '' : ` Не вдалося оновити: ${failedRoles.join(', ')}.`
    await load(true)
    say(failedRoles.length === 0 ? 'success' : 'info', base + extra)
    return true
  }

  async function save(): Promise<void> {
    const current = form.value
    const stored = detail.value
    if (busy.value || current === null || stored === null || !editable.value) return

    const problem = yearProblem(current.year)
    if (problem !== null) {
      fieldErrors.value = { year: problem }
      say('error', INVALID_YEAR_MESSAGE)
      return
    }

    busy.value = true
    notice.value = null
    try {
      const saved = await outcomeOf(
        client.PUT('/api/posts/{post_id}/fields', {
          params: { path: { post_id: postId } },
          body: toUpdate(current, revertNames.value, stored),
        }),
      )
      if (!saved.ok) {
        await fail(saved.failure)
        return
      }
      adopt(saved.data)
      schedulePreview()
      if (saved.data.published_at !== null) {
        say('info', 'Збережено. Підпис у каналі зміниться лише після «Застосувати в каналі».')
        return
      }
      const caption = saved.data.caption
      if (caption === null) {
        say('error', NOTHING_TO_APPLY_MESSAGE)
        return
      }
      await applyCaption(caption.sha256)
    } finally {
      busy.value = false
    }
  }

  async function applyToChannel(): Promise<void> {
    const caption = detail.value?.caption
    if (busy.value || caption === null || caption === undefined) return
    if (!(await deps.confirm(confirmWithHosts(CONFIRM_APPLY_TEXT, storedLinks())))) return

    busy.value = true
    notice.value = null
    try {
      await applyCaption(caption.sha256)
    } finally {
      busy.value = false
    }
  }

  async function archive(): Promise<void> {
    if (busy.value || !channelMissing.value) return
    if (!(await deps.confirm(CONFIRM_ARCHIVE_TEXT))) return

    busy.value = true
    notice.value = null
    try {
      const answer = await outcomeOf(
        client.POST('/api/posts/{post_id}/archive', { params: { path: { post_id: postId } } }),
      )
      if (!answer.ok) {
        await fail(answer.failure)
        return
      }
      archived.value = true
      channelMissing.value = false
      say('success', `Пост позначено: ${answer.data.status_label}.`)
    } finally {
      busy.value = false
    }
  }

  async function publishWithoutVideo(): Promise<void> {
    const stored = detail.value
    if (busy.value || stored === null || !canPublishCover.value) return
    if (!(await deps.confirm(confirmWithHosts(CONFIRM_COVER_TEXT, storedLinks())))) return

    busy.value = true
    notice.value = null
    try {
      const answer = await outcomeOf(
        client.POST('/api/posts/{post_id}/publish-cover', {
          params: { path: { post_id: postId } },
          body: { expected_post_updated_at: stored.expected_post_updated_at },
        }),
      )
      if (!answer.ok) {
        await fail(answer.failure)
        return
      }
      await load(true)
      say('success', COVER_PUBLISHING_MESSAGE)
    } finally {
      busy.value = false
    }
  }

  // «back to automatic» puts the stored value back, so the edit and the revert never disagree
  function toggleRevert(name: string): void {
    const stored = detail.value
    if (stored === null || !stored.manual_fields.includes(name)) return
    revert.value = revert.value.includes(name)
      ? revert.value.filter((entry) => entry !== name)
      : [...revert.value, name]
  }

  function dispose(): void {
    clearTimeout(previewTimer)
    previewSequence += 1
  }

  function errorOf(field: string): string | undefined {
    return fieldErrors.value[field]
  }

  return {
    status,
    loadMessage,
    detail,
    meta,
    form,
    revert,
    revertNames,
    fieldErrors,
    notice,
    preview,
    descriptionLinks,
    descriptionOverLimit,
    previewMessage,
    busy,
    channelMissing,
    archived,
    isPublished,
    editable,
    dirty,
    canApplyToChannel,
    coverAllowed,
    canPublishCover,
    warning,
    load,
    reloadIfClean,
    adoptFragmentSave,
    save,
    applyToChannel,
    archive,
    publishWithoutVideo,
    toggleRevert,
    errorOf,
    dispose,
  }
}
