// Everything the lyrics editor sets up outside Vue and has to undo when it goes away.

import { setClosingConfirmation } from '../telegram/actions'

export interface AudioLike {
  pause: () => void
  removeAttribute: (name: string) => void
  load: () => void
}

export interface EditorResources {
  detachKeys: () => void
  restoreSwipes: () => void
  audio: AudioLike | null
  webApp: TelegramWebApp | undefined
  setPlaying: (playing: boolean) => void
}

export function releaseEditor(resources: EditorResources): void {
  resources.detachKeys()
  resources.restoreSwipes()
  setClosingConfirmation(resources.webApp, false)
  resources.setPlaying(false)
  const media = resources.audio
  if (media === null) return
  media.pause()
  media.removeAttribute('src')
  media.load()
}
