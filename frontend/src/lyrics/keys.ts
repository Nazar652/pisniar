// Space taps and Backspace takes a tap back, but only when the focus is not in a field that types.

const NON_TEXT_INPUTS = new Set(['range', 'checkbox', 'radio', 'button', 'submit', 'reset'])
const TEXT_TAGS = new Set(['TEXTAREA', 'SELECT'])

export interface KeyHandlers {
  tap: () => void
  undo: () => void
}

export function isTextTarget(target: unknown): boolean {
  if (typeof target !== 'object' || target === null) return false
  const { tagName, type, isContentEditable } = target as {
    tagName?: unknown
    type?: unknown
    isContentEditable?: unknown
  }
  if (isContentEditable === true) return true
  if (typeof tagName !== 'string') return false
  if (TEXT_TAGS.has(tagName)) return true
  return tagName === 'INPUT' && !(typeof type === 'string' && NON_TEXT_INPUTS.has(type))
}

// a button or the slider has its own use of these keys, so they are left to it
export function isControlTarget(target: unknown): boolean {
  if (typeof target !== 'object' || target === null) return false
  const { tagName, type } = target as { tagName?: unknown; type?: unknown }
  return tagName === 'BUTTON' || (tagName === 'INPUT' && type === 'range')
}

function plainKey(event: KeyboardEvent): boolean {
  return (
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !isTextTarget(event.target) &&
    !isControlTarget(event.target)
  )
}

// the returned call removes the listeners again
export function attachKeys(host: EventTarget, handlers: KeyHandlers): () => void {
  // a handled Space is also cancelled on release; a lost release is forgotten on blur or any other key
  let spaceHandled = false
  const onBlur = (): void => {
    spaceHandled = false
  }
  const onKeyDown = (event: Event): void => {
    const key = event as KeyboardEvent
    const isSpace = key.key === ' '
    const handled = plainKey(key) && (isSpace || key.key === 'Backspace')
    spaceHandled = handled && isSpace
    if (!handled) return
    key.preventDefault()
    if (key.repeat) return
    if (isSpace) handlers.tap()
    else handlers.undo()
  }
  const onKeyUp = (event: Event): void => {
    if ((event as KeyboardEvent).key !== ' ' || !spaceHandled) return
    spaceHandled = false
    event.preventDefault()
  }
  host.addEventListener('keydown', onKeyDown)
  host.addEventListener('keyup', onKeyUp)
  host.addEventListener('blur', onBlur)
  return () => {
    host.removeEventListener('keydown', onKeyDown)
    host.removeEventListener('keyup', onKeyUp)
    host.removeEventListener('blur', onBlur)
  }
}
