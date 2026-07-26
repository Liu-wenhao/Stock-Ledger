/** Shared ECharts axis style: no horizontal grid lines. */
export const noSplitLine = { show: false }

export const axisLabelStyle = {
  color: '#787b86',
  fontSize: 11,
}

export const axisLineStyle = {
  lineStyle: { color: '#e0e3eb' },
}

export function moneyAxisFormatter(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return ''
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function pctAxisFormatter(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return ''
  return `${n.toFixed(2)}%`
}

export const zeroMarkLine = {
  silent: true,
  symbol: 'none',
  lineStyle: { color: '#d1d4dc', width: 1, type: 'solid' },
  data: [{ yAxis: 0 }],
  label: { show: false },
}
