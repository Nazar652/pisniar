// the shapes of Telegram's signed data and of our own tokens: nothing browser-side should ever carry one
export const FILTERED = '[Filtered]'
const PROPS_KEY = 'propsData'
const FILTERED_VALUES = [
  /eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g,
  /Bearer\s+\S+/g,
  /[?&]token=[^&\s'"]+/g,
  /tgWebAppData=[^\s'"]*/g,
  /\b(?:query_id|auth_date|hash|signature|chat_instance|chat_type|start_param|user)=[^\s'"&]*/g,
]

export function scrubText(text: string): string {
  return FILTERED_VALUES.reduce((scrubbed, pattern) => scrubbed.replace(pattern, FILTERED), text)
}

function scrubNode(node: unknown): unknown {
  if (typeof node === 'string') return scrubText(node)
  if (Array.isArray(node)) return node.map(scrubNode)
  if (node !== null && typeof node === 'object') {
    return Object.fromEntries(
      Object.entries(node).map(([key, value]) => [
        scrubText(key),
        key === PROPS_KEY ? FILTERED : scrubNode(value),
      ]),
    )
  }
  return node
}

// `beforeSend`: component props are dropped by key at any depth, every string by shape
export function scrubEvent<Event extends object>(event: Event): Event {
  return scrubNode(event) as Event
}
