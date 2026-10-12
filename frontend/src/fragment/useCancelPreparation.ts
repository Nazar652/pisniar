import { ref } from 'vue'
import type { ApiClient } from '../api/client'
import { outcomeOf } from '../api/outcome'

export const CANCELLED_MESSAGE = 'Підготовку скасовано. Пост знову чернетка.'

// gives up the prepared audio; the post goes back to a draft and the audio stays on the volume
export function useCancelPreparation(
  client: ApiClient,
  postId: number,
  postStamp: () => string,
  onFinished?: (cancelled: boolean) => void,
  beforeCancel?: () => Promise<boolean>,
) {
  const busy = ref(false)
  const failed = ref(false)
  const message = ref('')

  async function cancel(): Promise<void> {
    if (busy.value) return
    busy.value = true
    failed.value = false
    message.value = ''
    try {
      if (beforeCancel !== undefined && !(await beforeCancel())) return
      const answer = await outcomeOf(
        client.POST('/api/posts/{post_id}/fragment/cancel', {
          params: { path: { post_id: postId } },
          body: { expected_post_updated_at: postStamp() },
        }),
      )
      if (answer.ok) {
        message.value = CANCELLED_MESSAGE
        onFinished?.(true)
        return
      }
      failed.value = true
      message.value = answer.failure.message
      // the post is not as the screen shows it, so the screen is read again
      if (answer.failure.code === 'not_cancellable' || answer.failure.code === 'stale_data') {
        onFinished?.(false)
      }
    } finally {
      busy.value = false
    }
  }

  return { busy, failed, message, cancel }
}
