export type WebApp = TelegramWebApp

export interface TelegramHost {
  Telegram?: { WebApp?: TelegramWebApp }
}

export function telegramWebAppOf(host: TelegramHost): TelegramWebApp | undefined {
  return host.Telegram?.WebApp
}

// tells Telegram the page is ready and returns the raw `initData`, empty outside Telegram
export function startTelegram(webApp: TelegramWebApp | undefined): string {
  if (webApp === undefined) {
    return ''
  }
  webApp.ready()
  webApp.expand()
  return webApp.initData
}
