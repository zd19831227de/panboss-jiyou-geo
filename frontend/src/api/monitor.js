import api from './index'

export const runMonitor = (data) => api.post('/api/monitor/run', data)

export const getTaskStatus = (taskId) => api.get(`/api/monitor/task/${taskId}`)

export const getMonitorResults = (params) => api.get('/api/monitor/results', { params })

export const getMonitorHistory = (params) => api.get('/api/monitor/history', { params })

export const getAlerts = (params) => api.get('/api/monitor/alerts', { params })

export const getLoginStatus = () => api.get('/api/monitor/login-status')

export const getCaptchaStatus = () => api.get('/api/system/captcha-status')
