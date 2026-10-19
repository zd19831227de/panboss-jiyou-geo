import api from './index'

export const getKeywords = (params) => api.get('/api/keywords', { params })

export const getKeywordStats = (params) => api.get('/api/keywords/stats', { params })

export const getKeyword = (id) => api.get(`/api/keywords/${id}`)

export const createKeyword = (data) => api.post('/api/keywords', data)

export const updateKeyword = (id, data) => api.put(`/api/keywords/${id}`, data)

export const deleteKeyword = (id) => api.delete(`/api/keywords/${id}`)

export const importKeywords = (formData) => api.post('/api/keywords/import', formData, {
  headers: { 'Content-Type': 'multipart/form-data' }
})

export const exportKeywords = (params) => api.get('/api/keywords/export_csv', { params, responseType: 'blob' })

export const getKeywordsByBrand = (brandId) => api.get(`/api/brands/${brandId}/keywords`)
