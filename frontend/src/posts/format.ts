// «5 жовт., 14:30» in the viewer's time zone; an unreadable date gives an empty string
export function formatDate(iso: string | null, locale = 'uk-UA'): string {
  if (iso === null) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(locale, {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function joinArtists(names: readonly string[]): string {
  return names.length === 0 ? 'Без артиста' : names.join(', ')
}
