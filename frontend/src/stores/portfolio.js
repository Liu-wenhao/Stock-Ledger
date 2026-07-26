import { defineStore } from 'pinia'
import { ref } from 'vue'
import { fetchSummary } from '@/api/portfolio'

export const usePortfolioStore = defineStore('portfolio', () => {
  const summary = ref(null)
  const loading = ref(false)

  async function loadSummary() {
    loading.value = true
    try {
      summary.value = await fetchSummary()
    } finally {
      loading.value = false
    }
  }

  return { summary, loading, loadSummary }
})
