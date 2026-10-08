// the part of the Telegram Mini App SDK this app uses; members newer than the oldest client
// are optional and called only after `isVersionAtLeast`

interface TelegramBackButton {
  show: () => void
  hide: () => void
  onClick: (callback: () => void) => void
  offClick: (callback: () => void) => void
}

interface TelegramMainButton {
  text: string
  show: () => void
  hide: () => void
  enable: () => void
  disable: () => void
  showProgress: (leaveActive?: boolean) => void
  hideProgress: () => void
  onClick: (callback: () => void) => void
  offClick: (callback: () => void) => void
}

interface TelegramThemeParams {
  bg_color?: string
  text_color?: string
  hint_color?: string
  link_color?: string
  button_color?: string
  button_text_color?: string
  secondary_bg_color?: string
}

interface TelegramWebApp {
  initData: string
  version: string
  colorScheme: 'light' | 'dark'
  themeParams: TelegramThemeParams
  ready: () => void
  expand: () => void
  isVersionAtLeast: (version: string) => boolean
  onEvent: (eventType: string, handler: () => void) => void
  offEvent: (eventType: string, handler: () => void) => void
  showAlert: (message: string, callback?: () => void) => void
  showConfirm: (message: string, callback?: (confirmed: boolean) => void) => void
  BackButton: TelegramBackButton
  MainButton: TelegramMainButton
  HapticFeedback?: {
    impactOccurred: (style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft') => void
    notificationOccurred: (type: 'error' | 'success' | 'warning') => void
  }
}

interface Window {
  Telegram?: { WebApp?: TelegramWebApp }
}
