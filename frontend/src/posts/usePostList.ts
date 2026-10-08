import { ref } from 'vue'
import type { ApiClient } from '../api/client'
import { outcomeOf, type Schemas } from '../api/outcome'

export const PAGE_SIZE = 20

export type PostSummary = Schemas['PostSummary']
export type ListStatus = 'idle' | 'loading' | 'loaded' | 'failed'

export function usePostList(client: ApiClient) {
  const items = ref<PostSummary[]>([])
  const nextBeforeId = ref<number | null>(null)
  const status = ref<ListStatus>('idle')
  const loadingMore = ref(false)
  const message = ref('')

  async function fetchPage(beforeId: number | null) {
    const query =
      beforeId === null ? { limit: PAGE_SIZE } : { limit: PAGE_SIZE, before_id: beforeId }
    return outcomeOf(client.GET('/api/posts', { params: { query } }))
  }

  async function load(): Promise<void> {
    status.value = 'loading'
    message.value = ''
    const page = await fetchPage(null)
    if (!page.ok) {
      status.value = 'failed'
      message.value = page.failure.message
      return
    }
    items.value = page.data.items
    nextBeforeId.value = page.data.next_before_id
    status.value = 'loaded'
  }

  // a failed «more» keeps the cards already shown and offers the same button again
  async function loadMore(): Promise<void> {
    const cursor = nextBeforeId.value
    if (cursor === null || loadingMore.value) return
    loadingMore.value = true
    message.value = ''
    const page = await fetchPage(cursor)
    loadingMore.value = false
    if (!page.ok) {
      message.value = page.failure.message
      return
    }
    const known = new Set(items.value.map((item) => item.id))
    items.value = [...items.value, ...page.data.items.filter((item) => !known.has(item.id))]
    nextBeforeId.value = page.data.next_before_id
  }

  return { items, nextBeforeId, status, loadingMore, message, load, loadMore }
}
