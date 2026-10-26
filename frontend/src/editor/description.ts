import type { Schemas } from '../api/outcome'
import type { CaptionView } from './caption'

export type DescriptionDestination = Schemas['DescriptionDestination']

export const LINK_TEMPLATE_LABEL = 'текст'
export const LINK_TEMPLATE_ADDRESS = 'https://'

export function descriptionHelp(maxLinks: number): string {
  return `Посилання: [текст](https://…), не більше ${maxLinks}. Порожній рядок — новий абзац. Усе інше лишається звичайним текстом.`
}

// the real hosts the links go to (punycode for a non-Latin one), each once, in order
export function destinationHosts(links: readonly DescriptionDestination[]): string[] {
  return [...new Set(links.map((link) => link.host))]
}

export function destinationsNote(links: readonly DescriptionDestination[]): string | null {
  return links.length === 0 ? null : `Посилання ведуть на: ${destinationHosts(links).join(', ')}.`
}

export function destinationWarnings(links: readonly DescriptionDestination[]): string[] {
  return links.flatMap((link) =>
    link.warnings.map((warning) => `«${link.label}» (${link.host}): ${warning}.`),
  )
}

// a confirmation names the hosts too, so the admin sees where subscribers will be sent
export function confirmWithHosts(text: string, links: readonly DescriptionDestination[]): string {
  const note = destinationsNote(links)
  return note === null ? text : `${text} ${note}`
}

export interface TextEdit {
  text: string
  selectionStart: number
  selectionEnd: number
}

// characters as the server counts them (code points, line breaks as one)
export function descriptionLength(text: string): number {
  return [...text.replace(/\r\n?/g, '\n')].length
}

export function descriptionCounter(text: string, max: number): string {
  return `${descriptionLength(text)}/${max}`
}

// «Додати посилання»: the selection becomes the link text and the address is selected for typing;
// without a selection a template goes in at the cursor and its text is selected
export function wrapAsLink(text: string, start: number, end: number): TextEdit {
  const from = Math.max(0, Math.min(start, end, text.length))
  const to = Math.min(text.length, Math.max(start, end))
  const before = text.slice(0, from)
  const after = text.slice(to)
  const selected = text.slice(from, to)

  if (selected === '') {
    const link = `[${LINK_TEMPLATE_LABEL}](${LINK_TEMPLATE_ADDRESS})`
    const labelStart = before.length + 1
    return {
      text: before + link + after,
      selectionStart: labelStart,
      selectionEnd: labelStart + LINK_TEMPLATE_LABEL.length,
    }
  }
  const addressStart = before.length + selected.length + 3
  return {
    text: `${before}[${selected}](${LINK_TEMPLATE_ADDRESS})${after}`,
    selectionStart: addressStart,
    selectionEnd: addressStart + LINK_TEMPLATE_ADDRESS.length,
  }
}

// what the description cost the rest of the caption, from the latest preview; the text itself is never cut
export function descriptionBudgetNote(
  view: CaptionView | null,
  description: string,
  blamesDescription: boolean,
): string | null {
  if (view === null || description.trim() === '') return null
  if (view.over_limit) {
    // the server decides whether the description is the cause; otherwise the caption is long without it
    if (!blamesDescription) return null
    const excess = view.visible_length - view.limit
    return `Підпис задовгий на ${excess} символів навіть після скорочення жанрів і платформ. Скоротіть опис.`
  }
  const given = [
    view.dropped_genres.length > 0 ? 'жанри' : null,
    view.dropped_platforms.length > 0 ? 'платформи' : null,
    view.release_title_dropped ? 'назву релізу' : null,
  ].filter((name): name is string => name !== null)
  return given.length === 0 ? null : `Щоб опис помістився, з підпису прибрано: ${given.join(', ')}.`
}
