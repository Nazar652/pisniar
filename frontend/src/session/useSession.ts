import { ref, type Ref } from 'vue'
import {
  createApiClient,
  failureMessage,
  NETWORK_FAILURE_MESSAGE,
  type ApiClient,
} from '../api/client'

export type SessionState =
  { kind: 'loading' } | { kind: 'ready'; userId: number } | { kind: 'failed'; message: string }

export interface SessionOptions {
  baseUrl: string
  initData: string
  presetToken?: string
  fetch?: typeof fetch
}

export interface Session {
  state: Ref<SessionState>
  client: ApiClient
  signIn: () => Promise<void>
}

export const OPEN_FROM_TELEGRAM_MESSAGE = 'Відкрийте застосунок кнопкою в Telegram.'

// the token lives only in this closure: never in storage, never in the URL
export function useSession(options: SessionOptions): Session {
  let token: string | null = options.presetToken ?? null
  const state = ref<SessionState>({ kind: 'loading' })
  const client = createApiClient({
    baseUrl: options.baseUrl,
    getToken: () => token,
    ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
  })

  async function exchangeInitData(): Promise<string | null> {
    const { data, error, response } = await client.POST('/api/session', {
      body: { init_data: options.initData },
    })
    if (data === undefined) {
      state.value = { kind: 'failed', message: failureMessage(response.status, error) }
      return null
    }
    return data.token
  }

  async function signIn(): Promise<void> {
    state.value = { kind: 'loading' }
    try {
      if (token === null) {
        if (options.initData === '') {
          state.value = { kind: 'failed', message: OPEN_FROM_TELEGRAM_MESSAGE }
          return
        }
        token = await exchangeInitData()
        if (token === null) {
          return
        }
      }

      const { data, error, response } = await client.GET('/api/me')
      state.value =
        data === undefined
          ? { kind: 'failed', message: failureMessage(response.status, error) }
          : { kind: 'ready', userId: data.user_id }
    } catch {
      state.value = { kind: 'failed', message: NETWORK_FAILURE_MESSAGE }
    }
  }

  return { state, client, signIn }
}
