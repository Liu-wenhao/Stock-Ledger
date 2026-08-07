<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import dayjs from 'dayjs'
import { fetchHoldings, fetchTrades, refreshMarket } from '@/api/portfolio'
import SymbolCell from '@/components/SymbolCell.vue'
import { formatMoney, formatPct, formatQty, pnlClass, tickerOf, toNumber } from '@/utils/format'
import { isUsMarketOpen } from '@/utils/marketHours'
import { useTableScrollY } from '@/composables/useTableScroll'

const router = useRouter()

const emit = defineEmits(['changed'])
const subTab = ref('positions')
const keyword = ref('')
const includeClosed = ref(false)
const metric = ref('percent')
const perfFilter = ref('all')
const loading = ref(false)
const data = ref({ items: [], total_market_value: 0, cash: 0 })
const chartRef = ref(null)
const tableScrollY = useTableScrollY(420)
const tableScroll = computed(() => ({ x: 1300, y: tableScrollY.value }))
const pagination = reactive({
  current: 1,
  pageSize: 20,
  showSizeChanger: true,
  showTotal: (total) => `共 ${total} 条`,
  pageSizeOptions: ['10', '20', '50', '100'],
})

const tradeModalOpen = ref(false)
const tradeModalLoading = ref(false)
const tradeModalSymbol = ref('')
const symbolTrades = ref([])
/** trade id -> true when this sell closed a cycle */
const clearTradeIds = ref(new Set())

const priceModalOpen = ref(false)
const priceModalLoading = ref(false)
const priceModalSymbol = ref('')
const priceChartRef = ref(null)
const priceTrades = ref([])
/** ms timestamp for vertical dashed separator between 清仓 and 持仓 */
const cycleSplitMs = ref(null)
/** clicked row's open-position average cost */
const priceAvgCost = ref(null)
/** clicked row's last/close price; omit markLine when null */
const priceLastClose = ref(null)

const SIDE_LABELS = {
  Buy: '买入',
  Sell: '卖出',
  Dividend: '股息',
  Deposit: '入金',
  Withdrawal: '出金',
}

const SIDE_STEP_STATUS = {
  Buy: 'finish',
  Sell: 'error',
  Dividend: 'process',
}

let chart
let priceChart
let quoteRefreshTimer
let quoteRefreshBusy = false

const QUOTE_REFRESH_INTERVAL_MS = 10_000

function onTableChange(pag) {
  if (pag) {
    pagination.current = pag.current
    pagination.pageSize = pag.pageSize
  }
}

function sortByNumber(key) {
  return (a, b) => toNumber(a[key]) - toNumber(b[key])
}

/** Compact US-market quote time for the latest price; empty when unknown. */
function formatCloseDate(v) {
  if (!v) return ''
  const d = dayjs(v)
  return d.isValid() ? d.format('MM-DD HH:mm:ss') : ''
}

const reserved = ['financials', 'risk', 'technical']

async function load({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    const isClosed = subTab.value === 'closed'
    const isCumulative = subTab.value === 'cumulative'
    data.value = await fetchHoldings({
      view: isClosed ? 'closed' : isCumulative ? 'cumulative' : 'positions',
      // 持仓/价格始终只看当前持仓；表现页仍可用「显示已清仓」
      include_closed: subTab.value === 'performance' ? includeClosed.value : false,
      q: keyword.value || undefined,
    })
    if (subTab.value === 'performance') renderPerf()
  } finally {
    if (!silent) loading.value = false
  }
}

async function autoRefreshQuotes() {
  if (quoteRefreshBusy || loading.value || !isUsMarketOpen()) return
  quoteRefreshBusy = true
  try {
    await refreshMarket(true)
    await load({ silent: true })
    emit('changed')
  } catch {
    // Keep the timer alive; the next market-open interval can retry.
  } finally {
    quoteRefreshBusy = false
  }
}

function startQuoteRefreshTimer() {
  if (quoteRefreshTimer) return
  quoteRefreshTimer = window.setInterval(autoRefreshQuotes, QUOTE_REFRESH_INTERVAL_MS)
}

function stopQuoteRefreshTimer() {
  if (!quoteRefreshTimer) return
  window.clearInterval(quoteRefreshTimer)
  quoteRefreshTimer = undefined
}

function tradeInCycle(t, startMs, endMs) {
  const ts = new Date(t.closing_time).getTime()
  if (startMs != null && ts < startMs) return false
  if (endMs != null && ts > endMs) return false
  return true
}

async function openTradeHistory(record, cycle = null) {
  tradeModalSymbol.value = record.symbol
  tradeModalOpen.value = true
  tradeModalLoading.value = true
  symbolTrades.value = []
  clearTradeIds.value = new Set()

  try {
    const rows = await fetchTrades({
      side_group: 'all',
      symbol: record.symbol,
    })
    const cycles = cycle
      ? [cycle]
      : record.cycles?.length
        ? record.cycles
        : record.cycle_started_at || record.cycle_ended_at
          ? [{ started_at: record.cycle_started_at, ended_at: record.cycle_ended_at }]
          : null

    const filtered = [...rows]
      .filter((t) => {
        if (!['Buy', 'Sell', 'Dividend'].includes(t.side)) return false
        if (!cycles) {
          // 持仓：仅当前未清仓周期
          const cycleStart = record.cycle_started_at
            ? new Date(record.cycle_started_at).getTime()
            : null
          if (cycleStart == null) return true
          return new Date(t.closing_time).getTime() >= cycleStart
        }
        return cycles.some((c) =>
          tradeInCycle(
            t,
            c.started_at ? new Date(c.started_at).getTime() : null,
            c.ended_at ? new Date(c.ended_at).getTime() : null,
          ),
        )
      })
      .sort((a, b) => {
        const ta = new Date(a.closing_time).getTime()
        const tb = new Date(b.closing_time).getTime()
        return tb - ta || (b.id || 0) - (a.id || 0)
      })

    // Mark each cycle's closing Sell as 清仓
    if (cycles?.length) {
      const ids = new Set()
      for (const c of cycles) {
        if (!c.ended_at) continue
        const endMs = new Date(c.ended_at).getTime()
        const closer = filtered.find(
          (t) => t.side === 'Sell' && new Date(t.closing_time).getTime() === endMs,
        )
        if (closer?.id != null) ids.add(closer.id)
      }
      clearTradeIds.value = ids
    }

    symbolTrades.value = filtered
  } finally {
    tradeModalLoading.value = false
  }
}

function tradeStepTitle(t) {
  const base = `${SIDE_LABELS[t.side] || t.side}  ${formatMoney(t.fill_price, 'USD', 4).replace(' USD', '')} × ${formatQty(t.qty)}`
  if (clearTradeIds.value.has(t.id)) return `${base}  · 清仓`
  return base
}

function goEditTrade(t) {
  if (!t?.id) return
  tradeModalOpen.value = false
  router.push({ name: 'trades', query: { editId: String(t.id) } })
}

function tradeStepDesc(t) {
  const parts = [
    dayjs(t.closing_time).format('YYYY-MM-DD HH:mm'),
    `总额 ${formatMoney(t.total).replace(' USD', '')}`,
  ]
  if (toNumber(t.commission) > 0) {
    parts.push(`手续费 ${formatMoney(t.commission).replace(' USD', '')}`)
  }
  if (t.notes) parts.push(t.notes)
  return parts.join(' · ')
}

/**
 * Replay Buy/Sell qty chronologically; when qty hits 0 a cycle ends.
 * Separator sits between the last clearing sell and the first buy of the
 * current open cycle. No line if the symbol never fully cleared.
 */
function findCycleSplitMs(trades) {
  let qty = 0
  let lastClearMs = null
  let openCycleStartMs = null

  for (const t of trades) {
    const q = toNumber(t.qty)
    const ts = new Date(t.closing_time).getTime()
    if (t.side === 'Buy') {
      if (qty <= 0) openCycleStartMs = ts
      qty += q
    } else if (t.side === 'Sell') {
      qty -= Math.min(q, qty)
      if (qty <= 0) {
        qty = 0
        lastClearMs = ts
        openCycleStartMs = null
      }
    }
  }

  // Only draw when history has a closed cycle and a subsequent open cycle
  if (lastClearMs == null || openCycleStartMs == null || qty <= 0) return null
  if (openCycleStartMs <= lastClearMs) return lastClearMs
  return (lastClearMs + openCycleStartMs) / 2
}

async function openAvgPriceChart(record, cycle = null) {
  priceModalSymbol.value = record.symbol
  priceModalOpen.value = true
  priceModalLoading.value = true
  priceTrades.value = []
  cycleSplitMs.value = null
  priceAvgCost.value = record.avg_price != null ? toNumber(record.avg_price) : null
  const close = record.last_price != null ? toNumber(record.last_price) : null
  priceLastClose.value = close != null && Number.isFinite(close) ? close : null

  try {
    const rows = await fetchTrades({
      side_group: 'trades',
      symbol: record.symbol,
    })
    const trades = [...rows]
      .filter((t) => t.side === 'Buy' || t.side === 'Sell')
      .filter((t) => {
        if (!cycle) return true
        return tradeInCycle(
          t,
          cycle.started_at ? new Date(cycle.started_at).getTime() : null,
          cycle.ended_at ? new Date(cycle.ended_at).getTime() : null,
        )
      })
      .sort((a, b) => {
        const ta = new Date(a.closing_time).getTime()
        const tb = new Date(b.closing_time).getTime()
        return ta - tb || (a.id || 0) - (b.id || 0)
      })

    priceTrades.value = trades
    cycleSplitMs.value = findCycleSplitMs(trades)
    await nextTick()
    renderPriceChart()
  } finally {
    priceModalLoading.value = false
  }
}

function disposePriceChart() {
  if (priceChart) {
    priceChart.dispose()
    priceChart = null
  }
}

function renderPriceChart() {
  if (!priceChartRef.value) return
  if (!priceChart) priceChart = echarts.init(priceChartRef.value)

  const trades = priceTrades.value
  const lineData = trades.map((t) => [
    new Date(t.closing_time).getTime(),
    toNumber(t.fill_price),
  ])
  const qtys = trades.map((t) => toNumber(t.qty)).filter((n) => Number.isFinite(n) && n > 0)
  const qtyMin = qtys.length ? Math.min(...qtys) : 0
  const qtyMax = qtys.length ? Math.max(...qtys) : 0
  // Baseline = ECharts scatter default (no symbolSize before qty scaling).
  // minQty → base; larger qtys grow up (never shrink below base). Soften small ratios.
  const SYM_SIZE_BASE = 10
  const SYM_SIZE_MAX = 22
  const qtyToSymbolSize = (qty) => {
    const q = toNumber(qty)
    if (!Number.isFinite(q) || q <= 0) return SYM_SIZE_BASE
    if (qtyMax <= qtyMin) return SYM_SIZE_BASE
    const logMin = Math.log(qtyMin)
    const logMax = Math.log(qtyMax)
    const t = (Math.log(q) - logMin) / (logMax - logMin) // 0..1 on log qty
    // Full grow span only when qtyMax/qtyMin ≈ 8×; ~1.7× (3 vs 5) → ~0.24 damp.
    const ratio = qtyMax / qtyMin
    const damp = Math.min(1, Math.log2(ratio) / 3)
    return SYM_SIZE_BASE + t * (SYM_SIZE_MAX - SYM_SIZE_BASE) * damp
  }
  const points = trades.map((t) => {
    const isBuy = t.side === 'Buy'
    const color = isBuy ? '#089981' : '#f23645'
    return {
      value: [new Date(t.closing_time).getTime(), toNumber(t.fill_price)],
      side: t.side,
      qty: t.qty,
      symbolSize: qtyToSymbolSize(t.qty),
      itemStyle: { color },
      label: {
        show: true,
        formatter: isBuy ? 'B' : 'S',
        color,
        fontSize: 12,
        fontWeight: 700,
        position: 'top',
        distance: 4,
      },
    }
  })

  const split = cycleSplitMs.value
  const avg = priceAvgCost.value
  const close = priceLastClose.value
  const markLineData = []
  if (split != null) {
    markLineData.push({
      xAxis: split,
      label: {
        show: true,
        formatter: '清仓 | 持仓',
        color: '#787b86',
        fontSize: 11,
        position: 'insideEndTop',
      },
      lineStyle: {
        type: 'dashed',
        color: '#787b86',
        width: 1.5,
      },
    })
  }
  if (avg != null && Number.isFinite(avg)) {
    markLineData.push({
      yAxis: avg,
      label: {
        show: true,
        formatter: `均价 ${formatMoney(avg, 'USD', 4).replace(' USD', '')}`,
        color: '#e6a700',
        fontSize: 11,
        position: 'insideStartTop',
      },
      lineStyle: {
        type: 'dashed',
        color: '#e6a700',
        width: 1.5,
      },
    })
  }
  if (close != null && Number.isFinite(close)) {
    markLineData.push({
      yAxis: close,
      label: {
        show: true,
        formatter: `价格 ${formatMoney(close, 'USD', 4).replace(' USD', '')}`,
        color: '#26a69a',
        fontSize: 11,
        position: 'insideStartTop',
      },
      lineStyle: {
        type: 'solid',
        color: '#26a69a',
        width: 1.5,
      },
    })
  }
  const markLine = markLineData.length
    ? {
        symbol: 'none',
        silent: true,
        data: markLineData,
      }
    : undefined

  // ECharts does not expand axis range for markLine; include avg/close so they aren't clipped
  const yExtent = trades
    .map((t) => toNumber(t.fill_price))
    .filter((n) => Number.isFinite(n))
  if (avg != null && Number.isFinite(avg)) yExtent.push(avg)
  if (close != null && Number.isFinite(close)) yExtent.push(close)
  let yAxisMin
  let yAxisMax
  if (yExtent.length) {
    const lo = Math.min(...yExtent)
    const hi = Math.max(...yExtent)
    const pad = Math.max((hi - lo) * 0.08, Math.abs(hi || lo || 1) * 0.002, 0.01)
    yAxisMin = lo - pad
    yAxisMax = hi + pad
  }

  priceChart.setOption(
    {
      animationDuration: 300,
      grid: { left: 16, right: 48, top: 36, bottom: 40, containLabel: true },
      tooltip: {
        trigger: 'item',
        backgroundColor: '#fff',
        borderColor: '#e0e3eb',
        textStyle: { color: '#131722', fontSize: 12 },
        formatter: (p) => {
          const d = p?.data
          if (!d || !d.value) return ''
          const [ts, price] = d.value || []
          const side = d.side === 'Buy' ? '买入' : '卖出'
          return [
            `${side} ${d.side === 'Buy' ? 'B' : 'S'}`,
            dayjs(ts).format('YYYY-MM-DD HH:mm'),
            `成交价 ${formatMoney(price, 'USD', 4).replace(' USD', '')}`,
            `数量 ${formatQty(d.qty)}`,
          ].join('<br/>')
        },
      },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: '#e0e3eb' } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: {
          color: '#787b86',
          fontSize: 11,
          hideOverlap: true,
          // Day only; show M/D when month changes. Cache survives ECharts re-measure passes.
          formatter: (() => {
            const cache = new Map()
            let prevMonthKey = null
            let prevValue = null
            return (v) => {
              if (cache.has(v)) return cache.get(v)
              if (prevValue != null && v < prevValue) {
                cache.clear()
                prevMonthKey = null
              }
              const d = dayjs(v)
              const monthKey = d.year() * 12 + d.month()
              const label =
                prevMonthKey !== monthKey
                  ? `${d.month() + 1}/${d.date()}`
                  : String(d.date())
              prevMonthKey = monthKey
              prevValue = v
              cache.set(v, label)
              return label
            }
          })(),
        },
      },
      yAxis: {
        type: 'value',
        scale: true,
        min: yAxisMin,
        max: yAxisMax,
        position: 'right',
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: '#f0f3f6', type: 'dashed' } },
        axisLabel: {
          color: '#787b86',
          fontSize: 11,
          formatter: (v) => Number(v).toFixed(2),
        },
      },
      series: [
        {
          type: 'line',
          data: lineData,
          showSymbol: false,
          symbol: 'none',
          clip: false,
          lineStyle: { color: '#2962ff', width: 2 },
          itemStyle: { color: '#2962ff' },
          markLine,
          tooltip: { show: false },
          z: 1,
        },
        {
          type: 'scatter',
          data: points,
          z: 2,
        },
      ],
    },
    true,
  )
  priceChart.resize()
}

const positionColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 220 },
  { title: '交易笔数', key: 'trade_count', dataIndex: 'trade_count', width: 100, align: 'right' },
  { title: '分配 %', dataIndex: 'allocation', key: 'allocation', align: 'right' },
  { title: '数量', dataIndex: 'quantity', key: 'quantity', align: 'right' },
  { title: '平均价格', dataIndex: 'avg_price', key: 'avg_price', align: 'right' },
  { title: '价格', dataIndex: 'last_price', key: 'last_price', align: 'right' },
  { title: '成本值', dataIndex: 'cost_basis', key: 'cost_basis', align: 'right' },
  {
    title: '值',
    dataIndex: 'market_value',
    key: 'market_value',
    align: 'right',
    sorter: sortByNumber('market_value'),
  },
  {
    title: '累计盈亏',
    dataIndex: 'total_pnl',
    key: 'total_pnl',
    align: 'right',
    sorter: sortByNumber('total_pnl'),
  },
  {
    title: '累计盈亏 %',
    dataIndex: 'total_pnl_percent',
    key: 'total_pnl_percent',
    align: 'right',
    sorter: sortByNumber('total_pnl_percent'),
  },
]

const priceColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 220 },
  { title: '价格', dataIndex: 'last_price', key: 'last_price', align: 'right' },
  { title: '日涨跌', dataIndex: 'day_change', key: 'day_change', align: 'right' },
  { title: '日涨跌 %', dataIndex: 'day_change_percent', key: 'day_change_percent', align: 'right' },
  { title: '开盘', dataIndex: 'open_price', key: 'open_price', align: 'right' },
  { title: '最高', dataIndex: 'high_price', key: 'high_price', align: 'right' },
  { title: '最低', dataIndex: 'low_price', key: 'low_price', align: 'right' },
  { title: '昨收', dataIndex: 'prev_close', key: 'prev_close', align: 'right' },
]

const closedColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 220 },
  { title: '周期数', key: 'cycle_count', dataIndex: 'cycle_count', width: 90, align: 'right' },
  { title: '交易笔数', key: 'trade_count', dataIndex: 'trade_count', width: 100, align: 'right' },
  {
    title: '买入金额',
    key: 'buy_amount',
    dataIndex: 'buy_amount',
    align: 'right',
    sorter: sortByNumber('buy_amount'),
  },
  {
    title: '卖出金额',
    key: 'sell_amount',
    dataIndex: 'sell_amount',
    align: 'right',
    sorter: sortByNumber('sell_amount'),
  },
  {
    title: '成本值',
    key: 'realized_cost',
    dataIndex: 'realized_cost',
    align: 'right',
    sorter: sortByNumber('realized_cost'),
  },
  {
    title: '清仓盈亏',
    key: 'realized_pnl',
    dataIndex: 'realized_pnl',
    align: 'right',
    sorter: sortByNumber('realized_pnl'),
  },
  {
    title: '清仓盈亏 %',
    key: 'realized_pnl_percent',
    dataIndex: 'realized_pnl_percent',
    align: 'right',
    sorter: sortByNumber('realized_pnl_percent'),
  },
]

const cycleColumns = [
  { title: '周期', key: 'cycle_index', dataIndex: 'cycle_index', width: 70, align: 'right' },
  { title: '建仓时间', key: 'started_at', dataIndex: 'started_at' },
  { title: '清仓时间', key: 'ended_at', dataIndex: 'ended_at' },
  { title: '交易笔数', key: 'trade_count', dataIndex: 'trade_count', width: 100, align: 'right' },
  { title: '买入金额', key: 'buy_amount', dataIndex: 'buy_amount', align: 'right' },
  { title: '卖出金额', key: 'sell_amount', dataIndex: 'sell_amount', align: 'right' },
  { title: '成本值', key: 'realized_cost', dataIndex: 'realized_cost', align: 'right' },
  { title: '清仓盈亏', key: 'realized_pnl', dataIndex: 'realized_pnl', align: 'right' },
  { title: '清仓盈亏 %', key: 'realized_pnl_percent', dataIndex: 'realized_pnl_percent', align: 'right' },
]

const cumulativeColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 220 },
  { title: '周期数', key: 'cycle_count', dataIndex: 'cycle_count', width: 90, align: 'right' },
  { title: '交易笔数', key: 'trade_count', dataIndex: 'trade_count', width: 100, align: 'right' },
  { title: '平均价格', key: 'avg_price', dataIndex: 'avg_price', align: 'right' },
  { title: '价格', key: 'last_price', dataIndex: 'last_price', align: 'right' },
  { title: '成本值', key: 'cost', dataIndex: 'cost', align: 'right' },
  { title: '累计盈亏', key: 'total_pnl', dataIndex: 'total_pnl', align: 'right' },
  { title: '累计盈亏 %', key: 'total_pnl_percent', dataIndex: 'total_pnl_percent', align: 'right' },
]

const cumulativeCycleColumns = [
  { title: '周期', key: 'cycle_index', dataIndex: 'cycle_index', width: 70, align: 'right' },
  { title: '状态', key: 'status', dataIndex: 'status', width: 80 },
  { title: '建仓时间', key: 'started_at', dataIndex: 'started_at' },
  { title: '结束时间', key: 'ended_at', dataIndex: 'ended_at' },
  { title: '交易笔数', key: 'trade_count', dataIndex: 'trade_count', width: 100, align: 'right' },
  { title: '平均价格', key: 'avg_price', dataIndex: 'avg_price', align: 'right' },
  { title: '价格', key: 'price', dataIndex: 'price', align: 'right' },
  { title: '成本值', key: 'cost', dataIndex: 'cost', align: 'right' },
  { title: '周期盈亏', key: 'pnl', dataIndex: 'pnl', align: 'right' },
  { title: '周期盈亏 %', key: 'pnl_percent', dataIndex: 'pnl_percent', align: 'right' },
]

const columns = computed(() => {
  if (subTab.value === 'price') return priceColumns
  if (subTab.value === 'cumulative') return cumulativeColumns
  return positionColumns
})

function avgPriceClass(record) {
  const avg = toNumber(record?.avg_price)
  const price = toNumber(record?.last_price)
  if (record?.avg_price == null || record?.last_price == null) return ''
  if (avg > price) return 'pnl-down'
  if (avg < price) return 'pnl-up'
  return ''
}

const positionsSummary = computed(() => {
  // 当前持仓行合计；% = 盈亏合计 / 市值合计
  const items = data.value.items || []
  const sumKey = (key) => items.reduce((acc, row) => acc + toNumber(row[key]), 0)
  const pnl = sumKey('total_pnl')
  const marketValue = sumKey('market_value')
  return {
    trade_count: sumKey('trade_count'),
    allocation: sumKey('allocation'),
    quantity: sumKey('quantity'),
    cost_basis: sumKey('cost_basis'),
    market_value: marketValue,
    total_pnl: pnl,
    total_pnl_percent: marketValue > 0 ? (pnl / marketValue) * 100 : 0,
  }
})

const closedSummary = computed(() => {
  const d = data.value || {}
  const cost = toNumber(d.total_realized_cost)
  const pnl = toNumber(d.total_realized_pnl)
  return {
    cycle_count: (d.items || []).reduce((acc, row) => acc + toNumber(row.cycle_count), 0),
    trade_count: toNumber(d.total_trade_count),
    buy_amount: toNumber(d.total_buy_amount),
    sell_amount: toNumber(d.total_sell_amount),
    realized_cost: cost,
    realized_pnl: pnl,
    realized_pnl_percent: cost > 0 ? (pnl / cost) * 100 : 0,
  }
})

const cumulativeSummary = computed(() => ({
  cycle_count: (data.value.items || []).reduce((acc, row) => acc + toNumber(row.cycle_count), 0),
  trade_count: toNumber(data.value.total_trade_count),
  cost: toNumber(data.value.total_cost),
  total_pnl: toNumber(data.value.total_pnl),
  total_pnl_percent: toNumber(data.value.total_pnl_percent),
}))

const perfItems = computed(() => {
  let items = [...(data.value.items || [])]
  // 涨幅榜 / 跌幅榜：按收益率筛选
  if (perfFilter.value === 'gainers') {
    items = items.filter((i) => toNumber(i.total_pnl_percent) > 0)
  } else if (perfFilter.value === 'losers') {
    items = items.filter((i) => toNumber(i.total_pnl_percent) < 0)
  } else if (perfFilter.value === 'dividends') {
    // 股利：有股息记录的标的
    items = items.filter((i) => toNumber(i.dividends) > 0)
  }
  items.sort((a, b) => {
    const av = metric.value === 'percent' ? toNumber(a.total_pnl_percent) : toNumber(a.total_pnl)
    const bv = metric.value === 'percent' ? toNumber(b.total_pnl_percent) : toNumber(b.total_pnl)
    return bv - av
  })
  return items
})

const PERF_ROW_H = 52
const PERF_PAD_Y = 12

function formatPerfLabel(row) {
  if (metric.value === 'percent') return formatPct(row.total_pnl_percent)
  return formatMoney(row.total_pnl).replace(' USD', '')
}

function perfNameClass(row) {
  if (row.closed) return 'is-closed'
  const pnl = toNumber(row.total_pnl)
  if (pnl > 0) return 'is-up'
  if (pnl < 0) return 'is-down'
  return ''
}

const perfChartHeight = computed(() =>
  Math.max(320, perfItems.value.length * PERF_ROW_H + PERF_PAD_Y * 2),
)

async function renderPerf() {
  await nextTick()
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value)
  // category axis: first item at bottom → reverse so top matches sorted list
  const items = [...perfItems.value].reverse()
  const labels = items.map((i) => i.symbol)
  const values = items.map((i) =>
    metric.value === 'percent' ? toNumber(i.total_pnl_percent) : toNumber(i.total_pnl),
  )
  const nums = values.map((v) => Number(v) || 0)
  const dataMin = nums.length ? Math.min(0, ...nums) : 0
  const dataMax = nums.length ? Math.max(0, ...nums) : 0
  const span = Math.max(dataMax - dataMin, 1)
  // leave room outside the longest bars so end labels / axis ticks aren't clipped
  const axisPad = span * 0.08
  const axisMin = dataMin < 0 ? dataMin - axisPad : dataMin
  const axisMax = dataMax > 0 ? dataMax + axisPad : dataMax

  chartRef.value.style.height = `${perfChartHeight.value}px`
  chart.resize()

  chart.setOption(
    {
      grid: {
        left: 12,
        right: 12,
        top: PERF_PAD_Y,
        bottom: PERF_PAD_Y,
        containLabel: true,
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params) => {
          const p = params?.[0]
          if (!p) return ''
          const row = items[p.dataIndex]
          if (!row) return String(p.name)
          const ticker = row.symbol.split(':').pop()
          return [
            `${ticker}${row.name ? ` · ${row.name}` : ''}`,
            `累计盈亏：${formatPct(row.total_pnl_percent)}`,
            `金额：${formatMoney(row.total_pnl)}`,
            row.closed ? '状态：已清仓' : null,
          ]
            .filter(Boolean)
            .join('<br/>')
        },
      },
      xAxis: {
        type: 'value',
        min: axisMin,
        max: axisMax,
        splitLine: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: '#787b86',
          fontSize: 11,
          hideOverlap: true,
          formatter:
            metric.value === 'percent'
              ? (v) => `${Number(v).toFixed(2)}%`
              : (v) => Number(v).toFixed(2),
        },
      },
      yAxis: {
        type: 'category',
        data: labels,
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { show: false },
      },
      series: [
        {
          type: 'bar',
          barMaxWidth: 18,
          clip: false,
          data: values.map((v, idx) => {
            const row = items[idx]
            const color = v >= 0 ? '#089981' : '#f23645'
            return {
              value: Number(v.toFixed(2)),
              itemStyle: { color },
              label: {
                show: true,
                position: v >= 0 ? 'right' : 'left',
                distance: 8,
                formatter: () => formatPerfLabel(row),
                color,
                fontSize: 12,
              },
            }
          }),
        },
      ],
    },
    true,
  )
}

watch([subTab, includeClosed], () => {
  pagination.current = 1
  load()
})
watch([metric, perfFilter, () => data.value.items], () => {
  if (subTab.value === 'performance') renderPerf()
})
watch(priceModalOpen, (open) => {
  if (!open) disposePriceChart()
})
onMounted(() => {
  load()
  startQuoteRefreshTimer()
})
onUnmounted(() => {
  stopQuoteRefreshTimer()
  disposePriceChart()
  if (chart) {
    chart.dispose()
    chart = null
  }
})
</script>

<template>
  <div>
    <a-tabs v-model:activeKey="subTab" size="small">
      <a-tab-pane key="positions" tab="持仓" />
      <a-tab-pane key="cumulative" tab="累计" />
      <a-tab-pane key="closed" tab="清仓" />
      <a-tab-pane key="price" tab="价格" />
      <a-tab-pane key="performance" tab="表现" />
      <a-tab-pane key="financials" tab="财务指标" />
      <a-tab-pane key="risk" tab="风险" />
      <a-tab-pane key="technical" tab="技术指标" />
    </a-tabs>

    <div v-if="reserved.includes(subTab)" class="empty-tab">
      <a-empty description="即将推出（二期）" />
    </div>

    <template v-else-if="subTab === 'performance'">
      <div class="toolbar">
        <a-radio-group v-model:value="perfFilter" button-style="solid">
          <a-radio-button value="all">全部</a-radio-button>
          <a-radio-button value="gainers">涨幅榜</a-radio-button>
          <a-radio-button value="losers">跌幅榜</a-radio-button>
          <a-radio-button value="dividends">股利</a-radio-button>
        </a-radio-group>
        <div style="display: flex; gap: 12px; align-items: center">
          <span>显示已清仓</span>
          <a-switch v-model:checked="includeClosed" />
          <a-radio-group v-model:value="metric">
            <a-radio-button value="abs">绝对值</a-radio-button>
            <a-radio-button value="percent">百分比</a-radio-button>
          </a-radio-group>
        </div>
      </div>
      <a-empty v-if="!loading && !perfItems.length" description="暂无表现数据" />
      <div v-show="perfItems.length" class="perf-layout" :style="{ height: `${perfChartHeight}px` }">
        <div class="perf-symbols" :style="{ paddingTop: `${PERF_PAD_Y}px`, paddingBottom: `${PERF_PAD_Y}px` }">
          <div
            v-for="row in perfItems"
            :key="row.symbol"
            class="perf-symbol-row"
            :class="perfNameClass(row)"
            :style="{ height: `${PERF_ROW_H}px` }"
          >
            <SymbolCell :symbol="row.symbol" :name="row.name" :logo-url="row.logo_url" />
          </div>
        </div>
        <div ref="chartRef" class="perf-chart" />
      </div>
    </template>

    <template v-else-if="subTab === 'cumulative'">
      <div class="toolbar">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
      </div>
      <a-table
        :columns="cumulativeColumns"
        :data-source="data.items"
        :loading="loading"
        row-key="symbol"
        :pagination="pagination"
        :scroll="tableScroll"
        size="middle"
        children-column-name="__no_tree_children__"
        @change="onTableChange"
      >
        <template #expandedRowRender="{ record }">
          <a-table
            class="cycle-inner-table"
            :columns="cumulativeCycleColumns"
            :data-source="record.cycles || []"
            :pagination="false"
            size="small"
            row-key="cycle_index"
          >
            <template #bodyCell="{ column, record: cycle }">
              <template v-if="column.key === 'cycle_index'">#{{ cycle.cycle_index }}</template>
              <template v-else-if="column.key === 'started_at' || column.key === 'ended_at'">
                {{ cycle[column.key] ? dayjs(cycle[column.key]).format('YYYY-MM-DD HH:mm') : '—' }}
              </template>
              <template v-else-if="column.key === 'trade_count'">
                <a
                  v-if="cycle.trade_count > 0"
                  class="trade-count-link"
                  @click.prevent="openTradeHistory(record, cycle)"
                >{{ cycle.trade_count }}</a>
                <span v-else>0</span>
              </template>
              <template v-else-if="column.key === 'avg_price'">
                <a
                  v-if="cycle.avg_price != null"
                  class="trade-count-link"
                  :class="avgPriceClass({ avg_price: cycle.avg_price, last_price: cycle.price })"
                  @click.prevent="openAvgPriceChart({ ...record, avg_price: cycle.avg_price, last_price: cycle.price }, cycle)"
                >{{ formatMoney(cycle.avg_price).replace(' USD', '') }}</a>
                <span v-else>—</span>
              </template>
              <template v-else-if="column.key === 'price'">
                {{ cycle.price == null ? '—' : formatMoney(cycle.price).replace(' USD', '') }}
              </template>
              <template v-else-if="column.key === 'cost'">
                {{ formatMoney(cycle.cost) }}
              </template>
              <template v-else-if="column.key === 'pnl'">
                <span :class="pnlClass(cycle.pnl)">{{ formatMoney(cycle.pnl) }}</span>
              </template>
              <template v-else-if="column.key === 'pnl_percent'">
                <span :class="pnlClass(cycle.pnl_percent)">{{ formatPct(cycle.pnl_percent) }}</span>
              </template>
            </template>
          </a-table>
        </template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'symbol'">
            <SymbolCell :symbol="record.symbol" :name="record.name" :logo-url="record.logo_url" />
          </template>
          <template v-else-if="column.key === 'trade_count'">
            <a v-if="record.trade_count > 0" class="trade-count-link" @click.prevent="openTradeHistory(record)">
              {{ record.trade_count }}
            </a>
            <span v-else>0</span>
          </template>
          <template v-else-if="column.key === 'cycle_count'">{{ record.cycle_count }}</template>
          <template v-else-if="column.key === 'avg_price'">
            <a
              v-if="record.avg_price != null"
              class="trade-count-link"
              :class="avgPriceClass(record)"
              @click.prevent="openAvgPriceChart(record)"
            >{{ formatMoney(record.avg_price).replace(' USD', '') }}</a>
            <span v-else>—</span>
          </template>
          <template v-else-if="column.key === 'last_price'">
            <span v-if="record.last_price == null">—</span>
            <span v-else class="close-price-cell">
              {{ formatMoney(record.last_price).replace(' USD', '') }}
              <span
                v-if="formatCloseDate(record.price_as_of)"
                class="close-price-date"
                :title="dayjs(record.price_as_of).format('YYYY-MM-DD HH:mm:ss')"
              >({{ formatCloseDate(record.price_as_of) }})</span>
            </span>
          </template>
          <template v-else-if="column.key === 'cost'">{{ formatMoney(record.cost) }}</template>
          <template v-else-if="column.key === 'total_pnl'">
            <span :class="pnlClass(record.total_pnl)">{{ formatMoney(record.total_pnl) }}</span>
          </template>
          <template v-else-if="column.key === 'total_pnl_percent'">
            <span :class="pnlClass(record.total_pnl_percent)">{{ formatPct(record.total_pnl_percent) }}</span>
          </template>
        </template>
        <template #summary>
          <a-table-summary fixed>
            <a-table-summary-row class="holdings-summary-row">
              <a-table-summary-cell :index="0"><strong>汇总</strong></a-table-summary-cell>
              <a-table-summary-cell :index="1" align="right">—</a-table-summary-cell>
              <a-table-summary-cell :index="2" align="right">{{ cumulativeSummary.cycle_count }}</a-table-summary-cell>
              <a-table-summary-cell :index="3" align="right">{{ cumulativeSummary.trade_count }}</a-table-summary-cell>
              <a-table-summary-cell :index="4" align="right">—</a-table-summary-cell>
              <a-table-summary-cell :index="5" align="right">{{ formatMoney(cumulativeSummary.cost) }}</a-table-summary-cell>
              <a-table-summary-cell :index="6" align="right">
                <span :class="pnlClass(cumulativeSummary.total_pnl)">{{ formatMoney(cumulativeSummary.total_pnl) }}</span>
              </a-table-summary-cell>
              <a-table-summary-cell :index="7" align="right">
                <span :class="pnlClass(cumulativeSummary.total_pnl_percent)">{{ formatPct(cumulativeSummary.total_pnl_percent) }}</span>
              </a-table-summary-cell>
            </a-table-summary-row>
          </a-table-summary>
        </template>
      </a-table>
    </template>

    <template v-else-if="subTab === 'closed'">
      <div class="toolbar">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
      </div>
      <a-table
        :columns="closedColumns"
        :data-source="data.items"
        :loading="loading"
        row-key="symbol"
        :pagination="pagination"
        :scroll="tableScroll"
        size="middle"
        children-column-name="__no_tree_children__"
        @change="onTableChange"
      >
        <template #expandedRowRender="{ record }">
          <a-table
            class="cycle-inner-table"
            :columns="cycleColumns"
            :data-source="record.cycles || []"
            :pagination="false"
            size="small"
            row-key="cycle_index"
          >
            <template #bodyCell="{ column, record: cycle }">
              <template v-if="column.key === 'cycle_index'">
                #{{ cycle.cycle_index }}
              </template>
              <template v-else-if="column.key === 'started_at' || column.key === 'ended_at'">
                {{ cycle[column.key] ? dayjs(cycle[column.key]).format('YYYY-MM-DD HH:mm') : '—' }}
              </template>
              <template v-else-if="column.key === 'trade_count'">
                <a
                  v-if="cycle.trade_count > 0"
                  class="trade-count-link"
                  @click.prevent="openTradeHistory(record, cycle)"
                >
                  {{ cycle.trade_count }}
                </a>
                <span v-else>0</span>
              </template>
              <template v-else-if="['buy_amount', 'sell_amount', 'realized_cost'].includes(column.key)">
                {{ formatMoney(cycle[column.key]) }}
              </template>
              <template v-else-if="column.key === 'realized_pnl'">
                <span :class="pnlClass(cycle.realized_pnl)">{{ formatMoney(cycle.realized_pnl) }}</span>
              </template>
              <template v-else-if="column.key === 'realized_pnl_percent'">
                <span :class="pnlClass(cycle.realized_pnl_percent)">{{ formatPct(cycle.realized_pnl_percent) }}</span>
              </template>
            </template>
          </a-table>
        </template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'symbol'">
            <SymbolCell :symbol="record.symbol" :name="record.name" :logo-url="record.logo_url" />
          </template>
          <template v-else-if="column.key === 'trade_count'">
            <a
              v-if="record.trade_count > 0"
              class="trade-count-link"
              @click.prevent="openTradeHistory(record)"
            >
              {{ record.trade_count }}
            </a>
            <span v-else>0</span>
          </template>
          <template v-else-if="['buy_amount', 'sell_amount', 'realized_cost'].includes(column.key)">
            {{ formatMoney(record[column.key]) }}
          </template>
          <template v-else-if="column.key === 'realized_pnl'">
            <span :class="pnlClass(record.realized_pnl)">{{ formatMoney(record.realized_pnl) }}</span>
          </template>
          <template v-else-if="column.key === 'realized_pnl_percent'">
            <span :class="pnlClass(record.realized_pnl_percent)">
              {{ formatPct(record.realized_pnl_percent) }}
            </span>
          </template>
        </template>
        <template #summary>
          <a-table-summary fixed>
            <a-table-summary-row class="holdings-summary-row">
              <a-table-summary-cell :index="0" />
              <a-table-summary-cell :index="1"><strong>汇总</strong></a-table-summary-cell>
              <a-table-summary-cell :index="2" align="right">{{ closedSummary.cycle_count }}</a-table-summary-cell>
              <a-table-summary-cell :index="3" align="right">{{ closedSummary.trade_count }}</a-table-summary-cell>
              <a-table-summary-cell :index="4" align="right">{{ formatMoney(closedSummary.buy_amount) }}</a-table-summary-cell>
              <a-table-summary-cell :index="5" align="right">{{ formatMoney(closedSummary.sell_amount) }}</a-table-summary-cell>
              <a-table-summary-cell :index="6" align="right">{{ formatMoney(closedSummary.realized_cost) }}</a-table-summary-cell>
              <a-table-summary-cell :index="7" align="right">
                <span :class="pnlClass(closedSummary.realized_pnl)">{{ formatMoney(closedSummary.realized_pnl) }}</span>
              </a-table-summary-cell>
              <a-table-summary-cell :index="8" align="right">
                <span :class="pnlClass(closedSummary.realized_pnl_percent)">{{ formatPct(closedSummary.realized_pnl_percent) }}</span>
              </a-table-summary-cell>
            </a-table-summary-row>
          </a-table-summary>
        </template>
      </a-table>
    </template>

    <template v-else>
      <div class="toolbar">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
      </div>
      <a-table
        :columns="columns"
        :data-source="data.items"
        :loading="loading"
        row-key="symbol"
        :pagination="pagination"
        :scroll="tableScroll"
        size="middle"
        @change="onTableChange"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'symbol'">
            <SymbolCell :symbol="record.symbol" :name="record.name" :logo-url="record.logo_url" />
          </template>
          <template v-else-if="column.key === 'trade_count'">
            <a
              v-if="record.trade_count > 0"
              class="trade-count-link"
              @click.prevent="openTradeHistory(record)"
            >
              {{ record.trade_count }}
            </a>
            <span v-else>0</span>
          </template>
          <template v-else-if="column.key === 'allocation'">{{ formatPct(record.allocation).replace('+', '') }}</template>
          <template v-else-if="column.key === 'quantity'">{{ formatQty(record.quantity) }}</template>
          <template v-else-if="column.key === 'last_price'">
            <span v-if="record.last_price == null">—</span>
            <span v-else class="close-price-cell">
              {{ formatMoney(record.last_price).replace(' USD', '') }}
              <span
                v-if="formatCloseDate(record.price_as_of)"
                class="close-price-date"
                :title="dayjs(record.price_as_of).format('YYYY-MM-DD HH:mm:ss')"
              >({{ formatCloseDate(record.price_as_of) }})</span>
            </span>
          </template>
          <template v-else-if="column.key === 'avg_price'">
            <a
              v-if="record.avg_price != null && subTab === 'positions'"
              class="trade-count-link"
              :class="avgPriceClass(record)"
              @click.prevent="openAvgPriceChart(record)"
            >
              {{ formatMoney(record.avg_price).replace(' USD', '') }}
            </a>
            <span v-else>
              {{ record.avg_price == null ? '—' : formatMoney(record.avg_price).replace(' USD', '') }}
            </span>
          </template>
          <template v-else-if="['open_price', 'high_price', 'low_price', 'prev_close'].includes(column.key)">
            {{ record[column.key] == null ? '—' : formatMoney(record[column.key]).replace(' USD', '') }}
          </template>
          <template v-else-if="['cost_basis', 'market_value', 'day_change'].includes(column.key)">
            <span :class="column.key === 'day_change' ? pnlClass(record[column.key]) : ''">
              {{ record[column.key] == null ? '—' : formatMoney(record[column.key]) }}
            </span>
          </template>
          <template v-else-if="column.key === 'total_pnl'">
            <span :class="pnlClass(record.total_pnl)">{{ formatMoney(record.total_pnl) }}</span>
          </template>
          <template v-else-if="column.key === 'total_pnl_percent' || column.key === 'day_change_percent'">
            <span :class="pnlClass(record[column.key])">
              {{ record[column.key] == null ? '—' : formatPct(record[column.key]) }}
            </span>
          </template>
        </template>
        <template v-if="subTab === 'positions'" #summary>
          <a-table-summary fixed>
            <a-table-summary-row class="holdings-summary-row">
              <a-table-summary-cell :index="0">
                <strong>汇总</strong>
              </a-table-summary-cell>
              <a-table-summary-cell :index="1" align="right">
                {{ positionsSummary.trade_count }}
              </a-table-summary-cell>
              <a-table-summary-cell :index="2" align="right">
                {{ formatPct(positionsSummary.allocation).replace('+', '') }}
              </a-table-summary-cell>
              <a-table-summary-cell :index="3" align="right">
                {{ formatQty(positionsSummary.quantity) }}
              </a-table-summary-cell>
              <a-table-summary-cell :index="4" align="right">—</a-table-summary-cell>
              <a-table-summary-cell :index="5" align="right">—</a-table-summary-cell>
              <a-table-summary-cell :index="6" align="right">
                {{ formatMoney(positionsSummary.cost_basis) }}
              </a-table-summary-cell>
              <a-table-summary-cell :index="7" align="right">
                {{ formatMoney(positionsSummary.market_value) }}
              </a-table-summary-cell>
              <a-table-summary-cell :index="8" align="right">
                <span :class="pnlClass(positionsSummary.total_pnl)">
                  {{ formatMoney(positionsSummary.total_pnl) }}
                </span>
              </a-table-summary-cell>
              <a-table-summary-cell :index="9" align="right">
                <span :class="pnlClass(positionsSummary.total_pnl_percent)">
                  {{ formatPct(positionsSummary.total_pnl_percent) }}
                </span>
              </a-table-summary-cell>
            </a-table-summary-row>
          </a-table-summary>
        </template>
      </a-table>
    </template>

    <a-modal
      v-model:open="tradeModalOpen"
      :title="`${tickerOf(tradeModalSymbol)} 交易记录`"
      :footer="null"
      width="560px"
      centered
      destroy-on-close
    >
      <a-spin :spinning="tradeModalLoading">
        <a-empty v-if="!tradeModalLoading && !symbolTrades.length" description="暂无交易" />
        <a-steps
          v-else-if="symbolTrades.length"
          progress-dot
          direction="vertical"
          size="small"
          class="trade-steps"
        >
          <a-step
            v-for="t in symbolTrades"
            :key="t.id"
            :status="clearTradeIds.has(t.id) ? 'error' : SIDE_STEP_STATUS[t.side] || 'wait'"
            class="trade-step-clickable"
            @click="goEditTrade(t)"
          >
            <template #title>
              <a class="trade-step-link" @click.prevent="goEditTrade(t)">{{ tradeStepTitle(t) }}</a>
            </template>
            <template #description>
              <span class="trade-step-desc" @click="goEditTrade(t)">{{ tradeStepDesc(t) }}</span>
            </template>
          </a-step>
        </a-steps>
      </a-spin>
    </a-modal>

    <a-modal
      v-model:open="priceModalOpen"
      :title="`${tickerOf(priceModalSymbol)} 交易价格`"
      :footer="null"
      width="760px"
      centered
      destroy-on-close
      @afterOpenChange="(open) => open && nextTick(() => renderPriceChart())"
    >
      <a-spin :spinning="priceModalLoading">
        <a-empty v-if="!priceModalLoading && !priceTrades.length" description="暂无交易" />
        <div v-show="priceTrades.length" ref="priceChartRef" class="avg-price-chart" />
      </a-spin>
    </a-modal>
  </div>
</template>

<style scoped>
.close-price-cell {
  white-space: nowrap;
}

.close-price-date {
  margin-left: 6px;
  color: #787b86;
  font-size: 12px;
}

.trade-count-link {
  color: #2962ff;
  font-weight: 600;
  cursor: pointer;
}

.trade-count-link:hover {
  text-decoration: underline;
}

.trade-steps {
  max-height: 60vh;
  overflow: auto;
  padding-right: 8px;
}

.trade-steps :deep(.ant-steps-item-title) {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
}

.trade-steps :deep(.ant-steps-item-description) {
  font-size: 12px;
  color: #787b86;
  padding-bottom: 12px;
}

.trade-step-clickable {
  cursor: pointer;
}

.trade-step-link {
  color: inherit;
}

.trade-step-link:hover {
  color: #2962ff;
  text-decoration: underline;
}

.trade-step-desc {
  cursor: pointer;
}

.holdings-summary-row :deep(td) {
  font-weight: 600;
  background: #f7f8fa !important;
}

.cycle-inner-table {
  margin: 0;
  background: #fafbfc;
}

.cycle-inner-table :deep(.ant-table) {
  background: transparent;
}

.cycle-inner-table :deep(.ant-table-thead > tr > th) {
  background: #f0f3f6;
  font-size: 12px;
}

:deep(.ant-table-expanded-row > .ant-table-cell) {
  padding: 8px 0 !important;
}

.perf-layout {
  display: flex;
  align-items: stretch;
  width: 100%;
  min-height: 320px;
}

.perf-symbols {
  width: 220px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
}

.perf-symbol-row {
  display: flex;
  align-items: center;
  min-width: 0;
  padding-right: 8px;
}

.perf-symbol-row.is-up :deep(.code),
.perf-symbol-row.is-up :deep(.name) {
  color: #089981;
}

.perf-symbol-row.is-down :deep(.code),
.perf-symbol-row.is-down :deep(.name) {
  color: #f23645;
}

.perf-symbol-row.is-closed :deep(.code),
.perf-symbol-row.is-closed :deep(.name) {
  color: #787b86;
}

.perf-chart {
  flex: 1;
  min-width: 0;
  height: 100%;
}

.avg-price-chart {
  width: 100%;
  height: 380px;
}
</style>
