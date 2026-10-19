import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 端口与后端地址可用环境变量覆盖（由 start_services.py 自动注入）：
//   VITE_PORT        前端端口，默认 15173
//   VITE_API_TARGET  后端地址，默认 http://127.0.0.1:18770
// 也可以直接在 frontend/.env 里写 VITE_PORT / VITE_API_TARGET。
//
// 端口一律做校验：环境变量写成非数字会得到 NaN，Vite 会直接启动失败；
// 非法值统一回落默认端口，保证"写错也能起得来"。
function readPort(value, fallback) {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 && parsed < 65536 ? parsed : fallback
}
const frontendPort = readPort(process.env.VITE_PORT || process.env.GEO_FRONTEND_PORT, 15173)
const backendPort = readPort(process.env.GEO_BACKEND_PORT || process.env.PORT, 18770)
const apiTarget = process.env.VITE_API_TARGET || `http://127.0.0.1:${backendPort}`

export default defineConfig({
  plugins: [vue()],
  server: {
    port: frontendPort,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true
      },
      '/artifacts': {
        target: apiTarget,
        changeOrigin: true
      },
      '/screenshot': {
        target: apiTarget,
        changeOrigin: true
      }
    }
  }
})
