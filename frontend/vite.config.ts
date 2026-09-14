import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 生产环境 API 地址请通过环境变量 VITE_API_URL 配置（见 README），不要硬编码。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
})
