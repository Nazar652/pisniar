// Screens with unsaved work report it here; the Telegram back button asks once if any source is dirty.

import type { InjectionKey } from 'vue'
import type { Confirm } from '../telegram/actions'

export const DISCARD_QUESTION = 'Є незбережені зміни. Вийти без збереження?'

export type DirtySource = 'fields' | 'lyrics' | 'fragment'

export interface LeaveGuard {
  setDirty: (source: DirtySource, dirty: boolean) => void
  leave: (confirm: Confirm) => Promise<boolean>
  // whether a source other than `source` has unsaved work
  othersDirty: (source: DirtySource) => boolean
}

// an action of one screen that would drop another screen's unsaved work asks first; no guard, no question
export async function confirmDiscardOthers(
  guard: LeaveGuard | null,
  source: DirtySource,
  confirm: Confirm,
): Promise<boolean> {
  return guard === null || !guard.othersDirty(source) || (await confirm(DISCARD_QUESTION))
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
    othersDirty: (source) => [...dirty].some((entry) => entry !== source),
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
