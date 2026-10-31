import { createApp } from 'vue'
import App from './App.vue'
import { installSentry } from './sentry/install'
import './styles/base.css'

async function start(): Promise<void> {
  let presetToken: string | undefined
  if (import.meta.env.DEV) {
    const { installDevShim } = await import('./dev/shim')
    presetToken = installDevShim(window)
  }
  const app = createApp(App, presetToken === undefined ? {} : { presetToken })
  installSentry(app, document)
  app.mount('#app')
}

void start()
