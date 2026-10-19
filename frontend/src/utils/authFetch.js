// 统一的带鉴权 fetch：所有组件级请求必须走这里而不是裸 fetch。
// - 自动附带 Authorization: Bearer <geo_token>
// - 401 时清理 token 并跳转登录页（与 api/index.js 的 axios 拦截器行为一致）
// - 默认 60s 超时（AbortController），避免请求永久挂起
const DEFAULT_TIMEOUT_MS = 60000

export async function authFetch(url, options = {}) {
  const token = localStorage.getItem('geo_token')
  const headers = new Headers(options.headers || {})
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), options.timeout ?? DEFAULT_TIMEOUT_MS)
  let res
  try {
    res = await fetch(url, { ...options, headers, signal: controller.signal })
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时，请稍后重试')
    throw error
  } finally {
    clearTimeout(timer)
  }
  if (res.status === 401) {
    localStorage.removeItem('geo_token')
    if (window.location.pathname !== '/login') window.location.href = '/login'
    throw new Error('登录已过期，请重新登录')
  }
  return res
}

export default authFetch
