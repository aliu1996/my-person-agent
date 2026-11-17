import { fileURLToPath } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  root: fileURLToPath(new URL('./platform/', import.meta.url)),
  base: './',
  plugins: [vue()],
  build: { outDir: '../dist/platform', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      '/api/chat/stream': process.env.CHAT_STREAM_URL || 'http://127.0.0.1:8101',
      '/api': process.env.CHAT_API_URL || 'http://127.0.0.1:8100',
    },
  },
})
