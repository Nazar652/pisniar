// Screens with unsaved work report it here; the Telegram back button asks once if any source is dirty.

import type { InjectionKey } from 'vue'
import type { Confirm } from '../telegram/actions'

export const DISCARD_QUESTION = 'Є незбережені зміни. Вийти без збереження?'

export type DirtySource = 'fields' | 'lyrics' | 'fragment'

export interface LeaveGuard {
  setDirty: (source: DirtySource, dirty: boolean) => void
  leave: (confirm: Confirm) => Promise<boolean>
}

// `onChange` hears whether any source is dirty, e.g. to drive the closing confirmation
export function createLeaveGuard(onChange?: (anyDirty: boolean) => void): LeaveGuard {
  const dirty = new Set<DirtySource>()
  return {
    setDirty: (source, isDirty) => {
      const before = dirty.size > 0
      if (isDirty) dirty.add(source)
      else dirty.delete(source)
      if (before !== dirty.size > 0) onChange?.(dirty.size > 0)
    },
    leave: async (confirm) => dirty.size === 0 || (await confirm(DISCARD_QUESTION)),
  }
}

export const LEAVE_GUARD: InjectionKey<LeaveGuard> = Symbol('leave-guard')

// a second call while the first is still running does nothing; a failure never escapes
export function singleFlight(task: () => Promise<void>): () => void {
  let running = false
  return () => {
    if (running) return
    running = true
    void task()
      .catch(() => undefined)
      .finally(() => {
        running = false
      })
  }
}
