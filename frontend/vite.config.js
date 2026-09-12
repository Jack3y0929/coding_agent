import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 固定使用 IPv4 回环地址，避免 Windows 将 localhost 解析为 IPv6 ::1
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:8000',
        ws: true,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
  },
})
