import { defineStore } from 'pinia'
import { ref } from 'vue'
import { fetchSummary } from '@/api/portfolio'

const MARKET_KEY = 'ledger-market'
const MARKETS = ['us', 'cn', 'hk']

export const usePortfolioStore = defineStore('portfolio', () => {
  const saved = localStorage.getItem(MARKET_KEY)
  const summary = ref(null)
  const loading = ref(false)
  const market = ref(MARKETS.includes(saved) ? saved : 'us')

  function setMarket(next) {
    if (!MARKETS.includes(next) || next === market.value) return
    market.value = next
    localStorage.setItem(MARKET_KEY, next)
  }

  async function loadSummary() {
    loading.value = true
    try {
      summary.value = await fetchSummary()
    } finally {
      loading.value = false
    }
  }

  return { summary, loading, market, setMarket, loadSummary }
})
