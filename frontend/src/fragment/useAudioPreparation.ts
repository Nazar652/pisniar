import { ref, watch } from 'vue'
import type { ApiClient } from '../api/client'
import { outcomeOf } from '../api/outcome'

export type AudioPreparationStatus =
  'idle' | 'requesting' | 'preparing' | 'ready' | 'failed' | 'timed_out' | 'unavailable'
export type AudioPreparationState = ReturnType<typeof useAudioPreparation>

export const POLL_INTERVAL_MS = 3000
// the download takes up to a minute, so the wait allows it a good deal more
export const POLL_LIMIT_MS = 210_000

export const TIMED_OUT_MESSAGE =
  'Підготовка аудіо триває довше, ніж зазвичай. Перевірте ще раз за хвилину.'
export const PREPARATION_FAILED_MESSAGE =
  'Не вдалося підготувати аудіо. Спробуйте ще раз або повторіть команду /retry у чаті з ботом.'

// asks the server to download the audio, then watches the post until the fragment can be chosen
export function useAudioPreparation(client: ApiClient, postId: number, onReady?: () => void) {
  const status = ref<AudioPreparationStatus>('idle')
  const message = ref('')

  let timer: ReturnType<typeof setTimeout> | null = null
  let watchingSince = 0
  let disposed = false

  function schedule(): void {
    timer = setTimeout(() => void poll(), POLL_INTERVAL_MS)
  }

  // a failed read is only a missed tick: the limit, not one bad answer, ends the wait
  async function poll(): Promise<void> {
    timer = null
    const answer = await outcomeOf(
      client.GET('/api/posts/{post_id}', { params: { path: { post_id: postId } } }),
    )
    if (disposed) return
    if (answer.ok) {
      const { fragment } = answer.data
      if (fragment.choosable) {
        status.value = 'ready'
        onReady?.()
        return
      }
      if (!fragment.preparing) {
        // nothing to retry when the post simply moved on, e.g. to a render started in the chat
        status.value = fragment.preparable ? 'failed' : 'unavailable'
        message.value = fragment.reason ?? PREPARATION_FAILED_MESSAGE
        return
      }
    }
    if (Date.now() - watchingSince >= POLL_LIMIT_MS) {
      status.value = 'timed_out'
      message.value = TIMED_OUT_MESSAGE
      return
    }
    schedule()
  }

  // looks at the post at once: the audio may be ready already, or be prepared by someone else
  function resume(): void {
    if (disposed) return
    status.value = 'preparing'
    message.value = ''
    watchingSince = Date.now()
    void poll()
  }

  // a download started elsewhere is noticed when the post turns preparing after the mount
  function followPreparing(preparing: () => boolean): void {
    watch(preparing, (now) => {
      if (now && status.value === 'idle') resume()
    })
  }

  async function start(): Promise<void> {
    if (status.value === 'requesting' || status.value === 'preparing') return
    status.value = 'requesting'
    message.value = ''
    const answer = await outcomeOf(
      client.POST('/api/posts/{post_id}/fragment/audio', {
        params: { path: { post_id: postId } },
      }),
    )
    if (disposed) return
    if (answer.ok) {
      status.value = 'preparing'
      watchingSince = Date.now()
      schedule()
      return
    }
    if (answer.failure.code === 'not_preparable') {
      resume()
      return
    }
    status.value = 'failed'
    message.value = answer.failure.message
  }

  function dispose(): void {
    disposed = true
    if (timer !== null) clearTimeout(timer)
    timer = null
  }

  return { status, message, start, resume, followPreparing, dispose }
}
