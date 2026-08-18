import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// No dev proxy on purpose: the gateway enables CORS, and the dashboard talks to
// it via VITE_API_BASE_URL (or same-origin when that is empty).
export default defineConfig({
  plugins: [vue()],
  server: { port: 5173 },
  build: { outDir: 'dist' },
})
