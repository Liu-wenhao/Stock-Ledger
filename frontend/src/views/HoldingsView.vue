<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import dayjs from 'dayjs'
import { message } from 'ant-design-vue'
import { DownloadOutlined } from '@ant-design/icons-vue'
import { fetchHoldings, fetchTrades, refreshMarket } from '@/api/portfolio'
import SymbolCell from '@/components/SymbolCell.vue'
import { formatMoney, formatPct, formatQty, pnlClass, tickerOf, toNumber } from '@/utils/format'
import { isUsMarketOpen } from '@/utils/marketHours'
import { downloadTableImage } from '@/utils/tableImage'
import { useTableScrollY } from '@/composables/useTableScroll'

const router = useRouter()
const route = useRoute()

const emit = defineEmits(['changed'])
const isPricePage = computed(() => route.name === 'prices')
const subTab = ref('positions')
const keyword = ref('')
const includeClosed = ref(false)
const metric = ref('percent')
const perfFilter = ref('all')
const loading = ref(false)
const data = ref({ items: [], total_market_value: 0, cash: 0 })
const chartRef = ref(null)
const tableScrollY = useTableScrollY(420)
const viewportWidth = ref(window.innerWidth)
const tableScroll = computed(() => ({
  x:
    viewportWidth.value > 560
      ? 1500
      : subTab.value === 'cumulative'
        ? 760
        : subTab.value === 'closed'
          ? 820
          : subTab.value === 'price'
            ? 780
            : 1140,
  y: viewportWidth.value <= 560 ? undefined : tableScrollY.value,
}))
const pagination = reactive({
  current: 1,
  pageSize: 10,
  showSizeChanger: true,
  showTotal: (total) => `共 ${total} 条`,
  pageSizeOptions: ['10', '20', '50', '100'],
})
const sortState = reactive({ field: null, order: null })

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

async function onTableChange(pag, _filters, sorter) {
  if (pag) {
    pagination.current = pag.current
    pagination.pageSize = pag.pageSize
  }
  const field = sorter?.field
  if (!['total_pnl', 'total_pnl_percent'].includes(field)) return

  const order =
    sorter?.order === 'ascend' ? 'asc' : sorter?.order === 'descend' ? 'desc' : null
  if (sortState.field === field && sortState.order === order) return

  sortState.field = order ? field : null
  sortState.order = order
  pagination.current = 1
  await load()
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

async function load({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    const isClosed = subTab.value === 'closed'
    const isCumulative = subTab.value === 'cumulative'
    data.value = await fetchHoldings({
      view: isClosed ? 'closed' : isCumulative ? 'cumulative' : 'positions',
      include_closed: subTab.value === 'performance' ? includeClosed.value : false,
      q: keyword.value || undefined,
      sort_by: sortState.field || undefined,
      sort_order: sortState.order || undefined,
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

  const compact = window.innerWidth <= 560

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
      grid: {
        left: compact ? 2 : 16,
        right: compact ? 18 : 48,
        top: 36,
        bottom: compact ? 30 : 40,
        containLabel: true,
      },
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
          formatter: (v) => Number(v).toFixed(0),
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
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', minWidth: 160, fixed: 'left' },
  { title: '交易笔数', key: 'trade_count', dataIndex: 'trade_count', width: 100, align: 'right' },
  { title: '分配 %', dataIndex: 'allocation', key: 'allocation', align: 'right' },
  { title: '数量', dataIndex: 'quantity', key: 'quantity', align: 'right' },
  { title: '平均价格', dataIndex: 'avg_price', key: 'avg_price', align: 'right' },
  { title: '价格', dataIndex: 'last_price', key: 'last_price', align: 'right' },
  { title: '建议止损', dataIndex: 'suggested_stop_price', key: 'suggested_stop_price', align: 'right' },
  { title: '必须止损', dataIndex: 'mandatory_stop_price', key: 'mandatory_stop_price', align: 'right' },
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
    sorter: true,
  },
  {
    title: '累计盈亏 %',
    dataIndex: 'total_pnl_percent',
    key: 'total_pnl_percent',
    align: 'right',
    sorter: true,
  },
]

const priceColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', minWidth: 160, fixed: 'left' },
  { title: '价格', dataIndex: 'last_price', key: 'last_price', align: 'right' },
  { title: '日涨跌', dataIndex: 'day_change', key: 'day_change', align: 'right' },
  { title: '日涨跌 %', dataIndex: 'day_change_percent', key: 'day_change_percent', align: 'right' },
  { title: '开盘', dataIndex: 'open_price', key: 'open_price', align: 'right' },
  { title: '最高', dataIndex: 'high_price', key: 'high_price', align: 'right' },
  { title: '最低', dataIndex: 'low_price', key: 'low_price', align: 'right' },
  { title: '昨收', dataIndex: 'prev_close', key: 'prev_close', align: 'right' },
]

const closedColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', minWidth: 160, fixed: 'left' },
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
    title: '平均价格',
    key: 'avg_price',
    dataIndex: 'avg_price',
    align: 'right',
    sorter: sortByNumber('avg_price'),
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
  { title: '平均价格', key: 'avg_price', dataIndex: 'avg_price', align: 'right' },
  { title: '清仓盈亏', key: 'realized_pnl', dataIndex: 'realized_pnl', align: 'right' },
  { title: '清仓盈亏 %', key: 'realized_pnl_percent', dataIndex: 'realized_pnl_percent', align: 'right' },
]

const cumulativeColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', minWidth: 160, fixed: 'left' },
  { title: '周期数', key: 'cycle_count', dataIndex: 'cycle_count', width: 90, align: 'right' },
  { title: '交易笔数', key: 'trade_count', dataIndex: 'trade_count', width: 100, align: 'right' },
  { title: '平均价格', key: 'avg_price', dataIndex: 'avg_price', align: 'right' },
  { title: '价格', key: 'last_price', dataIndex: 'last_price', align: 'right' },
  { title: '成本值', key: 'cost', dataIndex: 'cost', align: 'right' },
  {
    title: '累计盈亏',
    key: 'total_pnl',
    dataIndex: 'total_pnl',
    align: 'right',
    sorter: true,
  },
  {
    title: '累计盈亏 %',
    key: 'total_pnl_percent',
    dataIndex: 'total_pnl_percent',
    align: 'right',
    sorter: true,
  },
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

const compactColumnWidths = {
  symbol: 136,
  cycle_index: 54,
  status: 62,
  started_at: 132,
  ended_at: 132,
  trade_count: 72,
  cycle_count: 64,
  allocation: 72,
  quantity: 76,
  avg_price: 86,
  last_price: 90,
  suggested_stop_price: 94,
  mandatory_stop_price: 94,
  price: 86,
  cost_basis: 92,
  cost: 92,
  market_value: 92,
  total_pnl: 94,
  total_pnl_percent: 86,
  day_change: 92,
  day_change_percent: 86,
  buy_amount: 94,
  sell_amount: 94,
  realized_pnl: 94,
  realized_pnl_percent: 86,
}

function responsiveColumns(list) {
  if (viewportWidth.value > 560) return list
  return list.map((column) => {
    const width = compactColumnWidths[column.key]
    return width
      ? {
          ...column,
          width,
          minWidth: undefined,
          fixed: column.key === 'symbol' ? undefined : column.fixed,
        }
      : column
  })
}

const displayClosedColumns = computed(() => responsiveColumns(closedColumns))
const displayCumulativeColumns = computed(() => responsiveColumns(cumulativeColumns))
const displayCycleColumns = computed(() => responsiveColumns(cycleColumns))
const displayCumulativeCycleColumns = computed(() => responsiveColumns(cumulativeCycleColumns))

const columns = computed(() => {
  if (subTab.value === 'price') return responsiveColumns(priceColumns)
  if (subTab.value === 'cumulative') return displayCumulativeColumns.value
  return responsiveColumns(positionColumns)
})

function avgPriceClass(record) {
  const avg = toNumber(record?.avg_price)
  const price = toNumber(record?.last_price)
  if (record?.avg_price == null || record?.last_price == null) return ''
  if (avg > price) return 'pnl-down'
  if (avg < price) return 'pnl-up'
  return ''
}

function holdingStopHit(record, stopKey) {
  if (record?.last_price == null || record?.[stopKey] == null) return false
  return toNumber(record.last_price) <= toNumber(record[stopKey])
}

function holdingStopTone(record, stopKey) {
  const mandatoryHit = holdingStopHit(record, 'mandatory_stop_price')
  if (stopKey === 'mandatory_stop_price') return mandatoryHit ? 'stop-danger' : ''
  return !mandatoryHit && holdingStopHit(record, 'suggested_stop_price')
    ? 'stop-warning'
    : ''
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
  // 汇总行只汇总它下面的行（搜索过滤后同理，与持仓表一致）。
  // % = 清仓盈亏合计 / 成本值合计，和每一行的 盈亏/成本 同口径，
  // 也就是各行的成本加权平均；不能用后端全量合计，否则一搜索就对不上行。
  const items = data.value.items || []
  const sumKey = (key) => items.reduce((acc, row) => acc + toNumber(row[key]), 0)
  const cost = sumKey('realized_cost')
  const pnl = sumKey('realized_pnl')
  return {
    cycle_count: sumKey('cycle_count'),
    trade_count: sumKey('trade_count'),
    buy_amount: sumKey('buy_amount'),
    sell_amount: sumKey('sell_amount'),
    realized_cost: cost,
    realized_pnl: pnl,
    realized_pnl_percent: cost > 0 ? (pnl / cost) * 100 : 0,
  }
})

const cumulativeSummary = computed(() => {
  // 同上：只汇总当前返回的行
  const items = data.value.items || []
  const sumKey = (key) => items.reduce((acc, row) => acc + toNumber(row[key]), 0)
  const cost = sumKey('cost')
  const pnl = sumKey('total_pnl')
  return {
    cycle_count: sumKey('cycle_count'),
    trade_count: sumKey('trade_count'),
    cost,
    total_pnl: pnl,
    total_pnl_percent: cost > 0 ? (pnl / cost) * 100 : 0,
  }
})

// ── 汇总行 ──────────────────────────────────────────────────────────────
// 每张表的汇总行只有这一份定义：表格的 #summary 和"下载图片"都读它，
// 省得两边各写一遍再走岔（单元格顺序必须严格等于上面列数组的顺序，
// 展开列占位不在这里，由模板自己补）。
const positionsSummaryCells = computed(() => [
  { text: '汇总', strong: true }, // 商品代码
  { text: String(positionsSummary.value.trade_count), align: 'right' }, // 交易笔数
  { text: formatPct(positionsSummary.value.allocation).replace('+', ''), align: 'right' }, // 分配 %
  { text: formatQty(positionsSummary.value.quantity), align: 'right' }, // 数量
  { text: '—', align: 'right' }, // 平均价格是各标的自己的价格，合计无意义
  { text: '—', align: 'right' }, // 价格同上
  { text: '—', align: 'right' }, // 建议止损按个股计算
  { text: '—', align: 'right' }, // 必须止损按个股成本计算
  { text: formatMoney(positionsSummary.value.cost_basis), align: 'right' }, // 成本值
  { text: formatMoney(positionsSummary.value.market_value), align: 'right' }, // 值
  // 累计盈亏
  {
    text: formatMoney(positionsSummary.value.total_pnl),
    tone: pnlClass(positionsSummary.value.total_pnl),
    align: 'right',
  },
  // 累计盈亏 %
  {
    text: formatPct(positionsSummary.value.total_pnl_percent),
    tone: pnlClass(positionsSummary.value.total_pnl_percent),
    align: 'right',
  },
])

const cumulativeSummaryCells = computed(() => [
  { text: '汇总', strong: true }, // 商品代码
  { text: String(cumulativeSummary.value.cycle_count), align: 'right' }, // 周期数
  { text: String(cumulativeSummary.value.trade_count), align: 'right' }, // 交易笔数
  { text: '—', align: 'right' }, // 平均价格
  { text: '—', align: 'right' }, // 价格
  { text: formatMoney(cumulativeSummary.value.cost), align: 'right' }, // 成本值
  // 累计盈亏
  {
    text: formatMoney(cumulativeSummary.value.total_pnl),
    tone: pnlClass(cumulativeSummary.value.total_pnl),
    align: 'right',
  },
  // 累计盈亏 %
  {
    text: formatPct(cumulativeSummary.value.total_pnl_percent),
    tone: pnlClass(cumulativeSummary.value.total_pnl_percent),
    align: 'right',
  },
])

const closedSummaryCells = computed(() => [
  { text: '汇总', strong: true }, // 商品代码
  { text: String(closedSummary.value.cycle_count), align: 'right' }, // 周期数
  { text: String(closedSummary.value.trade_count), align: 'right' }, // 交易笔数
  { text: formatMoney(closedSummary.value.buy_amount), align: 'right' }, // 买入金额
  { text: formatMoney(closedSummary.value.sell_amount), align: 'right' }, // 卖出金额
  { text: '—', align: 'right' }, // 平均价格
  // 清仓盈亏
  {
    text: formatMoney(closedSummary.value.realized_pnl),
    tone: pnlClass(closedSummary.value.realized_pnl),
    align: 'right',
  },
  // 清仓盈亏 %
  {
    text: formatPct(closedSummary.value.realized_pnl_percent),
    tone: pnlClass(closedSummary.value.realized_pnl_percent),
    align: 'right',
  },
])

// ── 下载图片 ────────────────────────────────────────────────────────────
// 单元格值和表格里的渲染保持一致（包括 null 显示 —、金额带 USD、红绿配色）。
const isBlank = (v) => v == null || v === ''
const imageMoney = (v) => (isBlank(v) ? '—' : formatMoney(v))
const imagePrice = (v) => (isBlank(v) ? '—' : formatMoney(v).replace(' USD', ''))
const imagePct = (v) => (isBlank(v) ? '—' : formatPct(v))
const imageSymbol = (r) => ({ text: tickerOf(r.symbol), secondary: r.name || '' })
const imagePnl = (v) => ({ text: imageMoney(v), tone: pnlClass(v) })
const imagePriceCell = (v, asOf) => {
  if (isBlank(v)) return { text: '—' }
  const at = formatCloseDate(asOf)
  return { text: imagePrice(v), secondary: at ? `(${at})` : '' }
}

const positionImageCells = {
  symbol: imageSymbol,
  trade_count: (r) => String(r.trade_count ?? 0),
  allocation: (r) => formatPct(r.allocation).replace('+', ''),
  quantity: (r) => formatQty(r.quantity),
  avg_price: (r) => imagePrice(r.avg_price),
  last_price: (r) => imagePriceCell(r.last_price, r.price_as_of),
  suggested_stop_price: (r) => imagePrice(r.suggested_stop_price),
  mandatory_stop_price: (r) => imagePrice(r.mandatory_stop_price),
  cost_basis: (r) => imageMoney(r.cost_basis),
  market_value: (r) => imageMoney(r.market_value),
  total_pnl: (r) => imagePnl(r.total_pnl),
  total_pnl_percent: (r) => ({ text: imagePct(r.total_pnl_percent), tone: pnlClass(r.total_pnl_percent) }),
}

const cumulativeImageCells = {
  symbol: imageSymbol,
  cycle_count: (r) => String(r.cycle_count ?? 0),
  trade_count: (r) => String(r.trade_count ?? 0),
  avg_price: (r) => imagePrice(r.avg_price),
  last_price: (r) => imagePriceCell(r.last_price, r.price_as_of),
  cost: (r) => imageMoney(r.cost),
  total_pnl: (r) => imagePnl(r.total_pnl),
  total_pnl_percent: (r) => ({ text: imagePct(r.total_pnl_percent), tone: pnlClass(r.total_pnl_percent) }),
}

const closedImageCells = {
  symbol: imageSymbol,
  cycle_count: (r) => String(r.cycle_count ?? 0),
  trade_count: (r) => String(r.trade_count ?? 0),
  buy_amount: (r) => imageMoney(r.buy_amount),
  sell_amount: (r) => imageMoney(r.sell_amount),
  avg_price: (r) => imagePrice(r.avg_price),
  realized_pnl: (r) => imagePnl(r.realized_pnl),
  realized_pnl_percent: (r) => ({ text: imagePct(r.realized_pnl_percent), tone: pnlClass(r.realized_pnl_percent) }),
}

const imageTabs = {
  positions: { title: '持仓', columns: positionColumns, cells: positionImageCells, summary: positionsSummaryCells },
  cumulative: {
    title: '累计',
    columns: cumulativeColumns,
    cells: cumulativeImageCells,
    summary: cumulativeSummaryCells,
  },
  closed: { title: '清仓', columns: closedColumns, cells: closedImageCells, summary: closedSummaryCells },
}

const imageExporting = ref(false)

async function handleDownloadImage(tab) {
  const spec = imageTabs[tab]
  const items = data.value.items || []
  if (!spec || !items.length || imageExporting.value) return
  imageExporting.value = true
  try {
    const search = keyword.value ? ` · 搜索「${keyword.value}」` : ''
    const ok = await downloadTableImage({
      title: spec.title,
      subtitle: `${dayjs().format('YYYY-MM-DD HH:mm')} · 共 ${items.length} 行${search}`,
      columns: spec.columns,
      // 直接铺全部行：分页是前端做的，图片不受当前页码限制
      rows: items.map((row) => spec.columns.map((column) => spec.cells[column.key](row))),
      summary: spec.summary.value.map((cell) => ({ text: cell.text, tone: cell.tone })),
      filename: `${spec.title}-${dayjs().format('YYYYMMDD-HHmmss')}`,
    })
    if (ok) message.success('图片已开始下载')
    else message.error('生成图片失败')
  } catch {
    message.error('生成图片失败')
  } finally {
    imageExporting.value = false
  }
}

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

function onViewportResize() {
  viewportWidth.value = window.innerWidth
  chart?.resize()
  priceChart?.resize()
  if (subTab.value === 'performance') renderPerf()
  if (priceModalOpen.value) renderPriceChart()
}

watch(
  () => route.name,
  (name) => {
    subTab.value = name === 'prices' ? 'price' : subTab.value === 'price' ? 'positions' : subTab.value
    sortState.field = null
    sortState.order = null
    pagination.current = 1
    load()
  },
)

watch([subTab, includeClosed], () => {
  if (isPricePage.value) return
  sortState.field = null
  sortState.order = null
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
  subTab.value = route.name === 'prices' ? 'price' : 'positions'
  load()
  startQuoteRefreshTimer()
  window.addEventListener('resize', onViewportResize)
})
onUnmounted(() => {
  stopQuoteRefreshTimer()
  window.removeEventListener('resize', onViewportResize)
  disposePriceChart()
  if (chart) {
    chart.dispose()
    chart = null
  }
})
</script>

<template>
  <div class="holdings-view">
    <a-tabs v-if="!isPricePage" v-model:activeKey="subTab" size="small">
      <a-tab-pane key="positions" tab="持仓" />
      <a-tab-pane key="cumulative" tab="累计" />
      <a-tab-pane key="closed" tab="清仓" />
      <a-tab-pane key="performance" tab="表现" />
    </a-tabs>

    <template v-if="!isPricePage && subTab === 'performance'">
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

    <template v-else-if="!isPricePage && subTab === 'cumulative'">
      <div class="toolbar">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
        <a-button
          :loading="imageExporting"
          :disabled="!data.items.length"
          @click="handleDownloadImage('cumulative')"
        >
          <DownloadOutlined /> 下载图片
        </a-button>
      </div>
      <div class="mobile-scroll-hint">左右滑动查看完整的周期数据</div>
      <a-table
        class="mobile-data-table holdings-table cumulative-table"
        :columns="displayCumulativeColumns"
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
            :columns="displayCumulativeCycleColumns"
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
            <span v-if="record.total_pnl_percent == null">—</span>
            <span v-else :class="pnlClass(record.total_pnl_percent)">{{ formatPct(record.total_pnl_percent) }}</span>
          </template>
        </template>
        <template #summary>
          <a-table-summary fixed>
            <a-table-summary-row class="holdings-summary-row">
              <!-- expandedRowRender 会让 ant-design-vue 在最前面插一个展开列，
                   所以先补一格占位，汇总内容按 cumulativeSummaryCells 的顺序铺 -->
              <a-table-summary-cell :index="0" />
              <a-table-summary-cell
                v-for="(cell, i) in cumulativeSummaryCells"
                :key="i"
                :index="i + 1"
                :align="cell.align"
              >
                <strong v-if="cell.strong">{{ cell.text }}</strong>
                <span v-else :class="cell.tone">{{ cell.text }}</span>
              </a-table-summary-cell>
            </a-table-summary-row>
          </a-table-summary>
        </template>
      </a-table>
    </template>

    <template v-else-if="!isPricePage && subTab === 'closed'">
      <div class="toolbar">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
        <a-button
          :loading="imageExporting"
          :disabled="!data.items.length"
          @click="handleDownloadImage('closed')"
        >
          <DownloadOutlined /> 下载图片
        </a-button>
      </div>
      <div class="mobile-scroll-hint">左右滑动查看买入、卖出和盈亏</div>
      <a-table
        class="mobile-data-table holdings-table closed-table"
        :columns="displayClosedColumns"
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
            :columns="displayCycleColumns"
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
              <template v-else-if="['buy_amount', 'sell_amount', 'avg_price'].includes(column.key)">
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
          <template v-else-if="['buy_amount', 'sell_amount', 'avg_price'].includes(column.key)">
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
              <!-- 同累计表：展开列占位一格，其余按 closedSummaryCells 的顺序铺 -->
              <a-table-summary-cell :index="0" />
              <a-table-summary-cell
                v-for="(cell, i) in closedSummaryCells"
                :key="i"
                :index="i + 1"
                :align="cell.align"
              >
                <strong v-if="cell.strong">{{ cell.text }}</strong>
                <span v-else :class="cell.tone">{{ cell.text }}</span>
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
        <a-button
          :loading="imageExporting"
          :disabled="!data.items.length"
          @click="handleDownloadImage('positions')"
        >
          <DownloadOutlined /> 下载图片
        </a-button>
      </div>
      <div class="mobile-scroll-hint">左右滑动查看价格、止损和盈亏</div>
      <a-table
        class="mobile-data-table holdings-table positions-table"
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
          <template v-else-if="['suggested_stop_price', 'mandatory_stop_price'].includes(column.key)">
            <span class="holding-stop-price" :class="holdingStopTone(record, column.key)">
              {{ record[column.key] == null ? '—' : formatMoney(record[column.key]).replace(' USD', '') }}
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
              <!-- 持仓表没有展开列，汇总内容按 positionsSummaryCells 的顺序直接铺 -->
              <a-table-summary-cell
                v-for="(cell, i) in positionsSummaryCells"
                :key="i"
                :index="i"
                :align="cell.align"
              >
                <strong v-if="cell.strong">{{ cell.text }}</strong>
                <span v-else :class="cell.tone">{{ cell.text }}</span>
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
      wrap-class-name="mobile-sheet-modal holdings-trade-modal"
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
      wrap-class-name="mobile-sheet-modal holdings-price-modal"
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

@media (max-width: 800px) {
  .holdings-view :deep(.ant-radio-group) {
    max-width: 100%;
  }

  .holdings-view :deep(.ant-radio-button-wrapper) {
    min-height: 40px;
    padding-inline: 11px;
    line-height: 38px;
  }

  .holdings-view .toolbar :deep(.ant-input-search) {
    width: 100% !important;
  }

  .perf-layout {
    min-height: 280px;
  }

  .perf-symbols {
    width: 150px;
  }

  .perf-symbol-row {
    padding-right: 6px;
  }

  .perf-chart {
    min-width: 190px;
  }

  .avg-price-chart {
    height: 320px;
  }

  .holdings-view :deep(.ant-table-cell-fix-left) {
    box-shadow: 8px 0 12px -12px rgba(19, 23, 34, 0.5);
  }
}

@media (max-width: 560px) {
  .holdings-view > .ant-tabs :deep(.ant-tabs-tab) {
    padding-inline: 6px !important;
  }

  .holdings-view .toolbar {
    margin-bottom: 10px;
  }

  .holdings-view .toolbar :deep(.ant-radio-group) {
    display: flex;
    width: 100%;
    overflow-x: auto;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
  }

  .holdings-view .toolbar :deep(.ant-radio-group::-webkit-scrollbar) {
    display: none;
  }

  .perf-layout {
    min-height: 250px;
  }

  .perf-symbols {
    width: 132px;
  }

  .perf-symbol-row {
    padding-right: 4px;
  }

  .perf-symbol-row :deep(.symbol-cell) {
    gap: 6px;
  }

  .perf-symbol-row :deep(.symbol-logo) {
    width: 24px;
    height: 24px;
  }

  .perf-symbol-row :deep(.symbol-meta .name) {
    max-width: 82px;
  }

  .trade-steps {
    max-height: calc(100dvh - 150px);
    padding-right: 2px;
  }

  .avg-price-chart {
    height: 280px;
  }
}

/* 持仓表：仅在价格触及相关止损位时着色，必须止损优先。 */
.holding-stop-price {
  font-variant-numeric: tabular-nums;
}
.holding-stop-price.stop-warning {
  color: #a96b00;
  font-weight: 650;
}
.holding-stop-price.stop-danger {
  color: #b52f27;
  font-weight: 750;
}
</style>
