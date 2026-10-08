import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// the prod policy lives in src/pisniar/webapp/app.py; this one only adds what the dev server needs
const DEV_CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'self' https://telegram.org",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self'",
  "connect-src 'self' ws://127.0.0.1:5173",
  "base-uri 'none'",
  "form-action 'none'",
].join('; ')

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: { '/api': 'http://127.0.0.1:18081' },
    headers: { 'Content-Security-Policy': DEV_CONTENT_SECURITY_POLICY },
  },
  test: { environment: 'node', include: ['tests/**/*.test.ts'], testTimeout: 20000 },
  build: {
    target: 'es2020',
    assetsInlineLimit: 0,
    sourcemap: false,
  },
})
