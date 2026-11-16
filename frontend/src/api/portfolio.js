import http from './http'

export const fetchSummary = () => http.get('/api/portfolio/summary').then((r) => r.data)
export const fetchNav = (params) => http.get('/api/portfolio/nav', { params }).then((r) => r.data)
export const fetchMovers = () => http.get('/api/portfolio/daily-movers').then((r) => r.data)
export const fetchDailyPnl = (params) =>
  http.get('/api/portfolio/daily-pnl', { params }).then((r) => r.data)
export const fetchDailyHoldings = (date) =>
  http.get('/api/portfolio/daily-holdings', { params: { date } }).then((r) => r.data)
export const fetchPeriodHoldings = (startDate, endDate) =>
  http.get('/api/portfolio/period-holdings', { params: { start_date: startDate, end_date: endDate } }).then((r) => r.data)
export const fetchPeriodPerformance = (params) =>
  http.get('/api/portfolio/period-performance', { params }).then((r) => r.data)
export const fetchHoldings = (params) => http.get('/api/holdings', { params }).then((r) => r.data)
export const fetchHoldingsAdvice = () => http.get('/api/holdings/advice').then((r) => r.data)
export const fetchIndicatorHistory = (symbol, params = {}) =>
  http.get('/api/holdings/indicator-history', { params: { symbol, limit: 40, ...params } }).then((r) => r.data)
export const fetchMarketSentiment = () => http.get('/api/market/sentiment').then((r) => r.data)
export const fetchCnDesk = () => http.get('/api/market/cn-desk').then((r) => r.data)
export const fetchCnFlow = () => http.get('/api/market/cn-flow', { timeout: 30000 }).then((r) => r.data)
export const fetchCnStockFlow = (symbols) =>
  http.get('/api/market/cn-stock-flow', { params: { symbols: symbols.join(',') }, timeout: 120000 }).then((r) => r.data)
export const fetchBreadthHistory = (book = '') =>
  http.get('/api/market/breadth-history', { params: book ? { book } : {} }).then((r) => r.data)
export const updateHoldingStop = (data) =>
  http.put('/api/holdings/stop', data).then((r) => r.data)
export const updateHoldingLimit = (data) =>
  http.put('/api/holdings/limit', data).then((r) => r.data)
export const updateHoldingSleeve = (data) =>
  http.put('/api/holdings/sleeve', data).then((r) => r.data)
export const fetchMonthlyReturns = (params) =>
  http.get('/api/analysis/monthly-returns', { params }).then((r) => r.data)
export const fetchProfitSeries = (params) =>
  http.get('/api/analysis/profit-series', { params }).then((r) => r.data)
export const fetchTradingReview = (params) =>
  http.get('/api/analysis/review', { params }).then((r) => r.data)
export const fetchTradingStrategy = (params) =>
  http.get('/api/analysis/strategy', { params }).then((r) => r.data)
export const fetchStrategyEngine = () =>
  http.get('/api/analysis/strategy/engine').then((r) => r.data)
export const fetchBacktest = (params) =>
  http.get('/api/analysis/backtest', { params, timeout: 180000 }).then((r) => r.data)
export const fetchBacktestCandles = (symbol, params) =>
  http.get('/api/analysis/backtest/candles', { params: { symbol, ...params }, timeout: 120000 }).then((r) => r.data)
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
