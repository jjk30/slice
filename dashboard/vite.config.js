import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// No dev proxy on purpose: the gateway enables CORS, and the dashboard talks to
// it via VITE_API_BASE_URL (or same-origin when that is empty).
export default defineConfig({
  // Relative base so the built index.html and its assets work wherever the build is
  // served from, including the gateway at /.
  base: './',
  plugins: [vue()],
  server: { port: 5173 },
  build: { outDir: 'dist' },
})
