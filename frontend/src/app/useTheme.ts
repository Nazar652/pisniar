export interface ThemeRoot {
  dataset: Record<string, string | undefined>
}

// Telegram sets the `--tg-theme-*` variables itself; this only mirrors the scheme for CSS
export function applyColorScheme(webApp: TelegramWebApp | undefined, root: ThemeRoot): void {
  root.dataset['colorScheme'] = webApp?.colorScheme ?? 'light'
}

// returns the unsubscribe call
export function useTheme(webApp: TelegramWebApp | undefined, root: ThemeRoot): () => void {
  applyColorScheme(webApp, root)
  if (webApp === undefined) return () => undefined

  const onChange = (): void => applyColorScheme(webApp, root)
  webApp.onEvent('themeChanged', onChange)
  return () => webApp.offEvent('themeChanged', onChange)
}
