import api from './index'

export const getBrands = () => api.get('/api/brands')

export const getBrand = (id) => api.get(`/api/brands/${id}`)

export const createBrand = (data) => api.post('/api/brands', data)

export const updateBrand = (id, data) => api.put(`/api/brands/${id}`, data)

export const deleteBrand = (id) => api.delete(`/api/brands/${id}`)

export const getBrandKeywordsCount = (brandId) => api.get(`/api/brands/${brandId}/keywords/count`)

export const getBrandCitations = (brandId) => api.get(`/api/brands/${brandId}/citations`)

export const getKeywordsByBrand = (brandId) => api.get(`/api/brands/${brandId}/keywords`)
