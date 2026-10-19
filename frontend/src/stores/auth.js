import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { login as apiLogin, register as apiRegister, getMe } from '../api/auth'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('geo_token') || '')
  const user = ref(null)
  const loading = ref(false)

  const isAuthenticated = computed(() => !!token.value)

  const setAuth = (newToken, newUser) => {
    token.value = newToken
    user.value = newUser
    localStorage.setItem('geo_token', newToken)
  }

  const login = async (username, password) => {
    loading.value = true
    try {
      const data = await apiLogin({ username, password })
      setAuth(data.access_token, data.user)
      return data
    } finally {
      loading.value = false
    }
  }

  const register = async (username, email, password) => {
    loading.value = true
    try {
      const data = await apiRegister({ username, email, password })
      return data
    } finally {
      loading.value = false
    }
  }

  const checkAuth = async () => {
    if (!token.value) return false
    try {
      const data = await getMe()
      if (data && data.username) {
        user.value = data
        return true
      }
    } catch {
      // Token invalid
    }
    token.value = ''
    user.value = null
    localStorage.removeItem('geo_token')
    return false
  }

  const logout = () => {
    token.value = ''
    user.value = null
    localStorage.removeItem('geo_token')
  }

  return { token, user, loading, isAuthenticated, login, register, checkAuth, logout }
})
