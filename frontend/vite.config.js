import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backend = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': backend,
      '/threads': backend,
      '/files': backend,
      '/healthz': backend,
    },
  },
})