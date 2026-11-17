import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  server: { port: 5174, proxy: { '/api/admin': process.env.CHAT_API_URL || 'http://127.0.0.1:8100' } },
})
