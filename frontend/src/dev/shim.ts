import type { TelegramHost } from '../telegram/useTelegram'

export const DEV_SHIM_MARKER = '__PISNIAR_DEV_SHIM__'
export const DEV_TOKEN_KEY = 'pisniar.devToken'

export interface DevShimHost extends TelegramHost {
  sessionStorage: Pick<Storage, 'getItem'>
  document: Pick<Document, 'createElement' | 'body'>
  confirm: (message: string) => boolean
}

function noop(): void {
  return undefined
}

// a stand-in for a Telegram button: a plain element, shown and hidden by the same calls
export function createDevButton(host: DevShimHost, label: string): TelegramMainButton {
  const element = host.document.createElement('button')
  element.hidden = true
  element.dataset['devShim'] = DEV_SHIM_MARKER
  element.textContent = label
  host.document.body.append(element)
  return {
    text: label,
    show: () => {
      element.hidden = false
    },
    hide: () => {
      element.hidden = true
    },
    enable: noop,
    disable: noop,
    showProgress: noop,
    hideProgress: noop,
    onClick: (callback) => element.addEventListener('click', callback),
    offClick: (callback) => element.removeEventListener('click', callback),
  }
}

// outside Telegram only: installs an SDK with empty `initData` and returns the stored real token
export function installDevShim(host: DevShimHost): string | undefined {
  if (host.Telegram?.WebApp === undefined) {
    host.Telegram = {
      WebApp: {
        initData: '',
        version: '9.0',
        colorScheme: 'light',
        themeParams: {},
        ready: noop,
        expand: noop,
        isVersionAtLeast: () => true,
        onEvent: noop,
        offEvent: noop,
        showAlert: noop,
        showConfirm: (message, callback) => callback?.(host.confirm(message)),
        BackButton: createDevButton(host, '←'),
        MainButton: createDevButton(host, 'OK'),
      },
    }
  }
  return host.sessionStorage.getItem(DEV_TOKEN_KEY) ?? undefined
}
