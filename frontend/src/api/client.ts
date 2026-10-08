import createClient, { type Middleware } from 'openapi-fetch'
import type { paths } from './schema'

export type ApiClient = ReturnType<typeof createClient<paths>>

export interface ClientOptions {
  baseUrl: string
  getToken: () => string | null
  fetch?: typeof fetch
}

const FAILURE_MESSAGES: Record<number, string> = {
  401: 'Сесія недійсна. Закрийте застосунок і відкрийте його знову.',
  403: 'Доступ лише для адміністраторів.',
  413: 'Запит завеликий.',
  422: 'Некоректний запит.',
  503: 'Сервер зараз недоступний. Спробуйте за хвилину.',
}
export const NETWORK_FAILURE_MESSAGE =
  'Немає зв’язку із сервером. Перевірте інтернет і спробуйте знову.'
export const UNKNOWN_FAILURE_MESSAGE = 'Щось пішло не так. Спробуйте ще раз.'

export function createApiClient(options: ClientOptions): ApiClient {
  const client = createClient<paths>({
    baseUrl: options.baseUrl,
    ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
  })
  const bearer: Middleware = {
    onRequest({ request }) {
      const token = options.getToken()
      if (token !== null) {
        request.headers.set('Authorization', `Bearer ${token}`)
      }
      return request
    },
  }
  client.use(bearer)
  return client
}

export function serverMessageOf(body: unknown): string | null {
  if (typeof body !== 'object' || body === null || !('message' in body)) {
    return null
  }
  const { message } = body as { message: unknown }
  return typeof message === 'string' && message !== '' ? message : null
}

// the server's own sentence wins; the status decides only when the body has none
export function failureMessage(status: number, body: unknown): string {
  return serverMessageOf(body) ?? FAILURE_MESSAGES[status] ?? UNKNOWN_FAILURE_MESSAGE
}
