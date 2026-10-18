import http from './http'

export const fetchSummary = () => http.get('/api/portfolio/summary').then((r) => r.data)
export const fetchNav = (params) => http.get('/api/portfolio/nav', { params }).then((r) => r.data)
export const fetchMovers = () => http.get('/api/portfolio/daily-movers').then((r) => r.data)
export const fetchDailyPnl = (params) =>
  http.get('/api/portfolio/daily-pnl', { params }).then((r) => r.data)
export const fetchTotalMovers = (params) =>
  http.get('/api/portfolio/total-movers', { params }).then((r) => r.data)
export const fetchPeriodPerformance = (params) =>
  http.get('/api/portfolio/period-performance', { params }).then((r) => r.data)
export const fetchHoldings = (params) => http.get('/api/holdings', { params }).then((r) => r.data)
export const fetchMonthlyReturns = (params) =>
  http.get('/api/analysis/monthly-returns', { params }).then((r) => r.data)
export const fetchProfitSeries = (params) =>
  http.get('/api/analysis/profit-series', { params }).then((r) => r.data)
export const fetchTradingReview = () => http.get('/api/analysis/review').then((r) => r.data)
export const fetchTradingStrategy = () => http.get('/api/analysis/strategy').then((r) => r.data)
export const fetchTrades = (params) => http.get('/api/trades', { params }).then((r) => r.data)
export const createTrade = (data) => http.post('/api/trades', data).then((r) => r.data)
export const updateTrade = (id, data) => http.put(`/api/trades/${id}`, data).then((r) => r.data)
export const deleteTrade = (id) => http.delete(`/api/trades/${id}`).then((r) => r.data)
export const importTrades = (file) => {
  const fd = new FormData()
  fd.append('file', file)
  return http.post('/api/trades/import', fd).then((r) => r.data)
}
export const exportTradesUrl = '/api/trades/export'
export const searchSymbols = (q) =>
  http.get('/api/symbols/search', { params: { q } }).then((r) => r.data)
export const refreshMarket = (force = true) =>
  http.post('/api/market/refresh', null, { params: { force } }).then((r) => r.data)
