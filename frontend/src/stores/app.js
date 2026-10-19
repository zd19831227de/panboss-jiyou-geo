import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useAppStore = defineStore('app', () => {
  const brands = ref([])
  const currentBrand = ref(null)
  const loading = ref(false)

  const setBrands = (list) => {
    brands.value = list
  }

  const setCurrentBrand = (brand) => {
    currentBrand.value = brand
  }

  const setLoading = (status) => {
    loading.value = status
  }

  return {
    brands,
    currentBrand,
    loading,
    setBrands,
    setCurrentBrand,
    setLoading
  }
})
