// A button or the slider keeps the focus after a mouse or touch use and would hold back the next space.

export const FOCUS_HOLDERS = 'button, input[type="range"]'

export function blurButton(target: unknown): void {
  if (typeof target !== 'object' || target === null) return
  const { closest } = target as { closest?: unknown }
  if (typeof closest !== 'function') return
  const holder = (closest as (selector: string) => { blur: () => void } | null).call(
    target,
    FOCUS_HOLDERS,
  )
  holder?.blur()
}

// a click made with the keyboard has no detail, so Tab then Space or Enter keeps its focus
export function blurAfterClick(event: { target: unknown; detail: number }): void {
  if (event.detail > 0) blurButton(event.target)
}
