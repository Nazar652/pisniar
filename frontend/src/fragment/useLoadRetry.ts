import { ref } from 'vue'

export const CHUNK_FAILED_MESSAGE =
  'Не вдалося завантажити редактор фрагмента. Перевірте інтернет і спробуйте знову.'

// the `onError` of an async component keeps its `retry`, so a button can ask for the chunk again
export function useLoadRetry() {
  const failed = ref(false)
  let pending: (() => void) | null = null

  function onError(_error: unknown, retry: () => void): void {
    failed.value = true
    pending = retry
  }

  function retry(): void {
    const again = pending
    pending = null
    failed.value = false
    again?.()
  }

  return { failed, onError, retry }
}
