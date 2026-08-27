import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// No dev proxy on purpose: the gateway enables CORS, and the dashboard talks to
// it via VITE_API_BASE_URL (or same-origin when that is empty).
export default defineConfig({
  // Relative base so the built index.html and every asset reference (JS, CSS, the
  // favicon) are './'-relative. The desktop app loads the bundled SPA over file://,
  // where a root-absolute '/assets/…' or '/favicon.png' resolves to the filesystem
  // root and 404s. This makes `npm run build` reproduce the bundled copy directly,
  // with no post-build path rewrite.
  base: './',
  plugins: [vue()],
  server: { port: 5173 },
  build: { outDir: 'dist' },
})
