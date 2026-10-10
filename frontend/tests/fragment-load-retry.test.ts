import { describe, expect, it, vi } from 'vitest'
import { useLoadRetry } from '../src/fragment/useLoadRetry'

describe('useLoadRetry', () => {
  it('starts without a failure', () => {
    expect(useLoadRetry().failed.value).toBe(false)
  })

  it('marks the failure and keeps the retry until the admin asks for it', () => {
    const chunk = useLoadRetry()
    const again = vi.fn()

    chunk.onError(new Error('offline'), again)

    expect(chunk.failed.value).toBe(true)
    expect(again).not.toHaveBeenCalled()
  })

  it('clears the failure and asks for the chunk again exactly once', () => {
    const chunk = useLoadRetry()
    const again = vi.fn()
    chunk.onError(new Error('offline'), again)

    chunk.retry()
    chunk.retry()

    expect(chunk.failed.value).toBe(false)
    expect(again).toHaveBeenCalledOnce()
  })

  it('shows a new failure when the retry fails again', () => {
    const chunk = useLoadRetry()
    chunk.onError(new Error('offline'), vi.fn())
    chunk.retry()

    chunk.onError(new Error('still offline'), vi.fn())

    expect(chunk.failed.value).toBe(true)
  })
})
