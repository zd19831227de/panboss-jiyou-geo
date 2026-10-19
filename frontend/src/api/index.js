import axios from 'axios'

const instance = axios.create({
  baseURL: '/',
  timeout: 60000,  // 60s for monitor operations
  headers: {
    'Content-Type': 'application/json'
  }
})

// Request interceptor — attach JWT token
instance.interceptors.request.use(
  config => {
    const token = localStorage.getItem('geo_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  error => Promise.reject(error)
)

// Response interceptor — extract data, handle 401
instance.interceptors.response.use(
  response => response.data,
  error => {
    if (error.response?.status === 401) {
      localStorage.removeItem('geo_token')
      // Only redirect to login if we're not already there
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    }
    console.error('API Error:', error)
    return Promise.reject(error)
  }
)

export default instance
