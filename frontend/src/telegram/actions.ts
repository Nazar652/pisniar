export type Confirm = (message: string) => Promise<boolean>
export type Haptic = (kind: 'success' | 'error') => void

// `showConfirm` answers through a callback; outside Telegram nothing can be confirmed
export function confirmWith(webApp: TelegramWebApp | undefined): Confirm {
  return (message) =>
    new Promise((resolve) => {
      if (webApp === undefined) {
        resolve(false)
        return
      }
      webApp.showConfirm(message, (confirmed) => resolve(confirmed === true))
    })
}

export function hapticWith(webApp: TelegramWebApp | undefined): Haptic {
  return (kind) => webApp?.HapticFeedback?.notificationOccurred(kind)
}

// a vertical swipe would close the app while a handle is dragged; returns the call that restores it
export function lockVerticalSwipes(webApp: TelegramWebApp | undefined): () => void {
  if (webApp === undefined || !webApp.isVersionAtLeast('7.7')) return () => undefined
  webApp.disableVerticalSwipes?.()
  return () => webApp.enableVerticalSwipes?.()
}

export interface MainButtonState {
  text: string
  visible: boolean
  enabled: boolean
  busy: boolean
}

// one place that maps the wanted state onto the Telegram button's calls
export function applyMainButton(button: TelegramMainButton, state: MainButtonState): void {
  button.text = state.text
  if (state.enabled) button.enable()
  else button.disable()
  if (state.busy) button.showProgress(false)
  else button.hideProgress()
  if (state.visible) button.show()
  else button.hide()
}
