// A screen that owns the whole page asks the post editor to keep Telegram's MainButton («Зберегти») hidden.

import { computed, ref, type ComputedRef, type InjectionKey } from 'vue'

export interface MainButtonGate {
  hidden: ComputedRef<boolean>
  // hides the button; the returned call gives the hold back, and only the first call counts
  hide: () => () => void
}

export function createMainButtonGate(): MainButtonGate {
  const holds = ref(0)
  return {
    hidden: computed(() => holds.value > 0),
    hide: () => {
      let held = true
      holds.value += 1
      return () => {
        if (!held) return
        held = false
        holds.value -= 1
      }
    },
  }
}

export const MAIN_BUTTON_GATE: InjectionKey<MainButtonGate> = Symbol('main-button-gate')
