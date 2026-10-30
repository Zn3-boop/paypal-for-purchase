import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // 所有业务 API 统一走 /api/v1，代理到本地 FastAPI 后端。
      '/api': 'http://127.0.0.1:8000',
    },
  },
})