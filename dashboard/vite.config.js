import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// Vue 3 dev/build config. The dashboard talks to the gateway's /api over HTTP
// (CORS is enabled gateway-side), so no dev proxy is needed — the base URL is
// configured via VITE_API_BASE (see .env.example), defaulting to localhost:8080.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: false,
  },
});
