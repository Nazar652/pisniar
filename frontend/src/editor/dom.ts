export function inputValue(event: Event): string {
  const target = event.target
  return target instanceof HTMLInputElement ||
    target instanceof HTMLSelectElement ||
    target instanceof HTMLTextAreaElement
    ? target.value
    : ''
}
