import api from './index'

export const listTasks = (params) => api.get('/api/tasks', { params })
export const getTask = (taskId) => api.get(`/api/tasks/${taskId}`)
export const cancelTask = (taskId) => api.post(`/api/tasks/${taskId}/cancel`)
