import api from './index'

export const login = (data) => api.post('/api/auth/login', data)

export const register = (data) => api.post('/api/auth/register', data)

export const getMe = () => api.get('/api/auth/me')
