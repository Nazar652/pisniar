import { computed, ref } from 'vue'

export const POST_ID_MAX = 9_223_372_036_854_775_807n

export type View = { kind: 'list' } | { kind: 'editor'; postId: number }

// `?post_id=N` opens the editor; anything that is not a plain positive id opens the list
export function postIdFromSearch(search: string): number | null {
  const raw = new URLSearchParams(search).get('post_id')
  if (raw === null || !/^[1-9]\d{0,15}$/.test(raw)) return null
  return BigInt(raw) <= POST_ID_MAX ? Number(raw) : null
}

export function useNavigation(search: string) {
  const initial = postIdFromSearch(search)
  const view = ref<View>(initial === null ? { kind: 'list' } : { kind: 'editor', postId: initial })
  const inEditor = computed(() => view.value.kind === 'editor')

  function openPost(postId: number): void {
    view.value = { kind: 'editor', postId }
  }

  function openList(): void {
    view.value = { kind: 'list' }
  }

  return { view, inEditor, openPost, openList }
}
