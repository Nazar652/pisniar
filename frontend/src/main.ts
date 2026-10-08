import { createApp } from 'vue'
import App from './App.vue'

async function start(): Promise<void> {
  let presetToken: string | undefined
  if (import.meta.env.DEV) {
    const { installDevShim } = await import('./dev/shim')
    presetToken = installDevShim(window)
  }
  createApp(App, presetToken === undefined ? {} : { presetToken }).mount('#app')
}

void start()
