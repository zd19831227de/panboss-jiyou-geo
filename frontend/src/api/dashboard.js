import api from './index'

// NOTE: backend query params use snake_case (brand_id), not camelCase
export const getDashboardStats = (brandId) => api.get('/api/dashboard/stats', { params: { brand_id: brandId } })

export const getMonthlyTrend = (brandId) => api.get('/api/dashboard/monthly-trend', { params: { brand_id: brandId } })

export const getRecentResults = (brandId, limit) => api.get('/api/dashboard/recent-results', { params: { brand_id: brandId, limit } })
