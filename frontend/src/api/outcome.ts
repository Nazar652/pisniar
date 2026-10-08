import { failureMessage, NETWORK_FAILURE_MESSAGE } from './client'
import type { components } from './schema'

export type Schemas = components['schemas']
export type FieldError = Schemas['FieldError']

export interface Failure {
  status: number
  code: string
  message: string
  fieldErrors: FieldError[]
}

export type Outcome<T> = { ok: true; data: T } | { ok: false; failure: Failure }

interface FetchResult<T> {
  data?: T | undefined
  error?: unknown
  response: Response
}

export const NETWORK_FAILURE: Failure = {
  status: 0,
  code: 'network',
  message: NETWORK_FAILURE_MESSAGE,
  fieldErrors: [],
}

function codeOf(body: unknown): string {
  if (typeof body === 'object' && body !== null && 'code' in body) {
    const { code } = body as { code: unknown }
    if (typeof code === 'string') return code
  }
  return 'unknown'
}

function fieldErrorsOf(body: unknown): FieldError[] {
  if (typeof body !== 'object' || body === null || !('errors' in body)) return []
  const { errors } = body as { errors: unknown }
  if (!Array.isArray(errors)) return []
  return errors.filter(
    (entry): entry is FieldError =>
      typeof entry === 'object' &&
      entry !== null &&
      typeof (entry as FieldError).field === 'string' &&
      typeof (entry as FieldError).message === 'string',
  )
}

export function failureOf(status: number, body: unknown): Failure {
  return {
    status,
    code: codeOf(body),
    message: failureMessage(status, body),
    fieldErrors: fieldErrorsOf(body),
  }
}

// a rejected request (no network) and an error answer end up in the same shape
export async function outcomeOf<T>(request: Promise<FetchResult<T>>): Promise<Outcome<T>> {
  try {
    const { data, error, response } = await request
    if (data !== undefined) return { ok: true, data }
    return { ok: false, failure: failureOf(response.status, error) }
  } catch {
    return { ok: false, failure: NETWORK_FAILURE }
  }
}
