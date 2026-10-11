import { describe, expect, it, vi } from 'vitest'
import { DISCARD_QUESTION, createLeaveGuard, singleFlight } from '../src/app/leaveGuard'
import { confirmWith } from '../src/telegram/actions'

describe('leave guard', () => {
  it('lets a clean screen go without asking', async () => {
    const confirm = vi.fn(async () => false)

    expect(await createLeaveGuard().leave(confirm)).toBe(true)
    expect(confirm).not.toHaveBeenCalled()
  })

  it('asks about unsaved work and stays on «no», leaves on «yes»', async () => {
    const guard = createLeaveGuard()
    guard.setDirty('lyrics', true)

    const no = vi.fn(async () => false)
    expect(await guard.leave(no)).toBe(false)
    expect(no).toHaveBeenCalledExactlyOnceWith(DISCARD_QUESTION)

    expect(await guard.leave(async () => true)).toBe(true)
  })

  it('stops asking once the work is saved or the screen is gone', async () => {
    const guard = createLeaveGuard()
    guard.setDirty('lyrics', true)
    guard.setDirty('lyrics', false)
    const confirm = vi.fn(async () => false)

    expect(await guard.leave(confirm)).toBe(true)
    expect(confirm).not.toHaveBeenCalled()
  })

  it('asks once when several sources are dirty, and until the last one is clean', async () => {
    const guard = createLeaveGuard()
    guard.setDirty('fields', true)
    guard.setDirty('lyrics', true)
    guard.setDirty('fragment', true)
    const confirm = vi.fn(async () => true)

    await guard.leave(confirm)
    expect(confirm).toHaveBeenCalledOnce()

    guard.setDirty('fields', false)
    guard.setDirty('lyrics', false)
    await guard.leave(confirm)
    expect(confirm).toHaveBeenCalledTimes(2)

    guard.setDirty('fragment', false)
    await guard.leave(confirm)
    expect(confirm).toHaveBeenCalledTimes(2)
  })

  it('does not count one source twice, so one clean report clears it', async () => {
    const guard = createLeaveGuard()
    guard.setDirty('fields', true)
    guard.setDirty('fields', true)
    guard.setDirty('fields', false)
    const confirm = vi.fn(async () => false)

    expect(await guard.leave(confirm)).toBe(true)
  })

  it('tells the change callback only when the overall state flips', () => {
    const onChange = vi.fn()
    const guard = createLeaveGuard(onChange)

    guard.setDirty('fields', true)
    guard.setDirty('lyrics', true)
    guard.setDirty('fields', false)
    guard.setDirty('lyrics', false)

    expect(onChange.mock.calls).toEqual([[true], [false]])
  })

  it('goes through Telegram showConfirm with the same question', async () => {
    const webApp = {
      showConfirm: vi.fn((_message: string, callback: (value: boolean) => void) => callback(false)),
    } as unknown as TelegramWebApp
    const guard = createLeaveGuard()
    guard.setDirty('fields', true)

    expect(await guard.leave(confirmWith(webApp))).toBe(false)
    expect(webApp.showConfirm).toHaveBeenCalledWith(DISCARD_QUESTION, expect.any(Function))
  })
})

describe('singleFlight', () => {
  it('does not start the task again while it runs, and allows it afterwards', async () => {
    let release: () => void = () => undefined
    const task = vi.fn(() => new Promise<void>((resolve) => (release = resolve)))
    const run = singleFlight(task)

    run()
    run()
    expect(task).toHaveBeenCalledOnce()
    release()
    await vi.waitFor(() => {
      run()
      expect(task).toHaveBeenCalledTimes(2)
    })
  })

  it('asks showConfirm once for a double back tap', async () => {
    const guard = createLeaveGuard()
    guard.setDirty('lyrics', true)
    let answer: (value: boolean) => void = () => undefined
    const webApp = {
      showConfirm: vi.fn((_message: string, callback: (value: boolean) => void) => {
        answer = callback
      }),
    } as unknown as TelegramWebApp
    const left = vi.fn()
    const back = singleFlight(async () => {
      if (await guard.leave(confirmWith(webApp))) left()
    })

    back()
    back()
    expect(webApp.showConfirm).toHaveBeenCalledOnce()
    answer(true)
    await vi.waitFor(() => expect(left).toHaveBeenCalledOnce())
  })

  it('swallows a failing task and runs again after it', async () => {
    // vitest fails the run on an unhandled rejection, so surviving this test is the assertion
    const task = vi.fn(async () => {
      throw new Error('boom')
    })
    const run = singleFlight(task)

    run()
    await vi.waitFor(() => {
      run()
      expect(task).toHaveBeenCalledTimes(2)
    })
    await new Promise((resolve) => setTimeout(resolve, 10))

    expect(task).toHaveBeenCalledTimes(2)
  })
})
