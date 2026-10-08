import type { Schemas } from '../api/outcome'

export type CaptionView = Schemas['CaptionView']

export type DisplaySegment =
  { kind: 'text'; text: string } | { kind: 'link'; text: string; href: string } | { kind: 'break' }

// only web links become anchors: anything else, whatever the server sent, stays plain text
export function safeHref(href: string | null): string | null {
  if (href === null) return null
  try {
    const { protocol } = new URL(href)
    return protocol === 'https:' || protocol === 'http:' ? href : null
  } catch {
    return null
  }
}

export function displaySegments(segments: CaptionView['segments']): DisplaySegment[] {
  return segments.map((segment): DisplaySegment => {
    if (segment.kind === 'line_break') return { kind: 'break' }
    const text = segment.text ?? ''
    const href = segment.kind === 'link' ? safeHref(segment.href) : null
    return href === null ? { kind: 'text', text } : { kind: 'link', text, href }
  })
}

export function lengthLabel(view: CaptionView): string {
  return `${view.visible_length}/${view.limit}`
}

// everything the caption could not hold, as sentences for the admin
export function droppedNotes(view: CaptionView): string[] {
  const notes: string[] = []
  if (view.dropped_genres.length > 0) {
    notes.push(`Жанри не влізли: ${view.dropped_genres.join(', ')}.`)
  }
  if (view.dropped_platforms.length > 0) {
    notes.push(`Платформи не влізли: ${view.dropped_platforms.join(', ')}.`)
  }
  if (view.release_title_dropped) notes.push('Назва релізу не влізла.')
  return [...notes, ...view.warnings]
}
