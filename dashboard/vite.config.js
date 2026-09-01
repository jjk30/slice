import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev proxy so the dashboard is same-origin with the gateway: the GitHub session cookie
// is httpOnly and same-origin, so every API and SSE call has to reach the gateway under
// the dev server's own origin. These prefixes are forwarded to the gateway on :8080.
const GATEWAY = 'http://localhost:8080'

export default defineConfig({
  // Relative base so the built index.html and its assets work wherever the build is
  // served from, including the gateway at /.
  base: './',
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
