export function toNumber(v, fallback = 0) {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

export function marketCurrency() {
  const market = localStorage.getItem('ledger-market') || 'us'
  return { us: 'USD', cn: 'CNY', hk: 'HKD' }[market] || 'USD'
}

function resolveCurrency(currency) {
  if (!currency || currency === 'USD') return marketCurrency()
  return currency
}

export function formatMoney(v, currency, digits = 2) {
  const unit = resolveCurrency(currency)
  const n = toNumber(v)
  const abs = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
  const sign = n < 0 ? '-' : ''
  return `${sign}${abs} ${unit}`
}

export function formatAmount(v, currency, digits = 2) {
  return formatMoney(v, currency, digits).replace(/ [A-Z]{3}$/, '')
}

export function formatPct(v, digits = 2) {
  const n = toNumber(v)
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}%`
}

export function formatQty(v) {
  const n = toNumber(v)
  if (Number.isInteger(n)) return String(n)
  return n.toLocaleString('en-US', { maximumFractionDigits: 4 })
}

export function pnlClass(v) {
  const n = toNumber(v)
  if (n > 0) return 'pnl-up'
  if (n < 0) return 'pnl-down'
  return ''
}

export function tickerOf(symbol) {
  if (!symbol) return ''
  const parts = String(symbol).split(':')
  return parts[parts.length - 1]
}
