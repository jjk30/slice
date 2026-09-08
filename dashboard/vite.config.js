import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev proxy so the dashboard is same-origin with the gateway: the GitHub session cookie
// is httpOnly and same-origin, so every API and SSE call has to reach the gateway under
// the dev server's own origin. These prefixes are forwarded to the gateway on :8080.
const GATEWAY = 'http://localhost:8080'

export default defineConfig({
  // Phase 30: the app is served at /dashboard and /settings as well as /, and the bundle
  // always lives at /assets on the same origin (app.main mounts it there), so the base
  // is absolute. A relative base would look for /dashboard/assets from a trailing-slash
  // URL and miss.
  base: '/',
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/auth': GATEWAY,
      '/dashboard': GATEWAY,
      '/admin': GATEWAY,
      '/account': GATEWAY,
      '/scanner': GATEWAY,
      '/v1': GATEWAY,
    },
  },
  build: { outDir: 'dist' },
})
