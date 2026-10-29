<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import dayjs from 'dayjs'
import { message, Modal } from 'ant-design-vue'
import { DashboardOutlined, DownloadOutlined, FallOutlined, LineChartOutlined, QuestionCircleOutlined, RiseOutlined } from '@ant-design/icons-vue'
import { fetchHoldings, fetchHoldingsAdvice, fetchIndicatorHistory, fetchMarketSentiment, fetchTrades, refreshMarket, updateHoldingLimit, updateHoldingStop } from '@/api/portfolio'
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
const loading = ref(false)
const data = ref({ items: [], total_market_value: 0, cash: 0 })
const tableScrollY = useTableScrollY(420)
const viewportWidth = ref(window.innerWidth)
const tableScroll = computed(() => ({
  x:
    viewportWidth.value > 560
      ? subTab.value === 'price'
        ? 1990
        : 1720
      : subTab.value === 'cumulative'
        ? 603
        : subTab.value === 'closed'
          ? 687
          : subTab.value === 'price'
            ? 2050
            : 1180,
  y: viewportWidth.value <= 560 ? undefined : tableScrollY.value,
}))
const expandColumnWidth = computed(() => (viewportWidth.value > 560 ? 36 : 24))
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

const addPositionOpen = ref(false)
const addPositionRecord = ref(null)
const addPositionForm = reactive({
  amount: null,
  targetPnlPct: null,
  stopPrice: null,
})

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


async function load({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    const isClosed = subTab.value === 'closed'
    const isCumulative = subTab.value === 'cumulative'
    data.value = await fetchHoldings({
      view: isClosed ? 'closed' : isCumulative ? 'cumulative' : 'positions',
      watch: isPricePage.value ? 1 : undefined,
      q: keyword.value || undefined,
      sort_by: sortState.field || undefined,
      sort_order: sortState.order || undefined,
    })
    if (!silent && (isPricePage.value || subTab.value === 'price')) {
      loadIndicators()
      loadSentiment()
    }
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
  const base = `${SIDE_LABELS[t.side] || t.side}  ${formatMoney(t.fill_price, 'USD', 4).replace(/ [A-Z]{3}$/, '')} × ${formatQty(t.qty)}`
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
    `总额 ${formatMoney(t.total).replace(/ [A-Z]{3}$/, '')}`,
  ]
  if (toNumber(t.commission) > 0) {
    parts.push(`手续费 ${formatMoney(t.commission).replace(/ [A-Z]{3}$/, '')}`)
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
        formatter: `均价 ${formatMoney(avg, 'USD', 4).replace(/ [A-Z]{3}$/, '')}`,
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
        formatter: `价格 ${formatMoney(close, 'USD', 4).replace(/ [A-Z]{3}$/, '')}`,
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
            `成交价 ${formatMoney(price, 'USD', 4).replace(/ [A-Z]{3}$/, '')}`,
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
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 126, minWidth: 126, fixed: 'left' },
  { title: '交易笔数', key: 'trade_count', dataIndex: 'trade_count', width: 100, align: 'right' },
  { title: '分配 %', dataIndex: 'allocation', key: 'allocation', align: 'right' },
  { title: '数量', dataIndex: 'quantity', key: 'quantity', align: 'right' },
  { title: '平均价格', dataIndex: 'avg_price', key: 'avg_price', align: 'right' },
  { title: '价格', dataIndex: 'last_price', key: 'last_price', align: 'right' },
  { title: '止损价格', key: 'stop_levels', dataIndex: 'stop_levels', width: 156, align: 'right' },
  { title: '仓位', key: 'position_limit', dataIndex: 'position_limit', width: 120, align: 'right' },
  { title: '成本值', dataIndex: 'cost_basis', key: 'cost_basis', align: 'right' },
  {
    title: '值',
    dataIndex: 'market_value',
    key: 'market_value',
    align: 'right',
    sorter: sortByNumber('market_value'),
  },
  {
    title: '盈亏',
    dataIndex: 'total_pnl',
    key: 'total_pnl',
    width: 128,
    align: 'right',
    sorter: true,
  },
  {
    title: '回本 %',
    key: 'breakeven_percent',
    dataIndex: 'breakeven_percent',
    width: 80,
    align: 'right',
  },
  {
    title: '盈亏 %',
    dataIndex: 'total_pnl_percent',
    key: 'total_pnl_percent',
    width: 80,
    align: 'right',
    sorter: true,
    fixed: 'right',
  },
  { title: '操作', key: 'actions', width: 64, align: 'center', fixed: 'right' },
]

const priceColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 120, fixed: 'left' },
  { title: '价格', dataIndex: 'last_price', key: 'last_price', align: 'right', width: 88 },
  { title: '日涨跌', dataIndex: 'day_change', key: 'day_change', align: 'right', width: 96 },
  { title: '日涨跌 %', dataIndex: 'day_change_percent', key: 'day_change_percent', align: 'right', width: 84 },
  { title: '开盘', dataIndex: 'open_price', key: 'open_price', align: 'right', width: 80 },
  { title: '最高', dataIndex: 'high_price', key: 'high_price', align: 'right', width: 80 },
  { title: '最低', dataIndex: 'low_price', key: 'low_price', align: 'right', width: 80 },
  { title: '昨收', dataIndex: 'prev_close', key: 'prev_close', align: 'right', width: 80 },
  { title: '成交量', key: 'volume', align: 'right', width: 100 },
  { title: '成交额', key: 'turnover', align: 'right', width: 100 },
  { title: 'Amihud', key: 'illiq', align: 'right', width: 96 },
  { title: '锚定日', key: 'anchor_date', align: 'right', width: 108 },
  { title: 'AVWAP', key: 'anchor_vwap', align: 'right', width: 100 },
  { title: '距锚', key: 'vs_anchor', align: 'right', width: 84 },
  { title: 'MA20', key: 'ma20', align: 'right', width: 96 },
  { title: '带宽', key: 'bandwidth', align: 'right', width: 84 },
  { title: '波动', key: 'vol', align: 'right', width: 84 },
  { title: '动量', key: 'mom', align: 'right', width: 80 },
  { title: '仓位上限', key: 'vol_cap', align: 'right', width: 100 },
  { title: '平价', key: 'rp_weight', align: 'right', width: 80 },
  { title: 'PE', key: 'pe', align: 'right', width: 72 },
  { title: 'ERP', key: 'erp', align: 'right', width: 80 },
]

const closedColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', minWidth: 96, fixed: 'left' },
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
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 126, minWidth: 126, fixed: 'left' },
  { title: '周期数', key: 'cycle_count', dataIndex: 'cycle_count', width: 90, align: 'right' },
  { title: '交易笔数', key: 'trade_count', dataIndex: 'trade_count', width: 100, align: 'right' },
  { title: '平均价格', key: 'avg_price', dataIndex: 'avg_price', align: 'right' },
  { title: '价格', key: 'last_price', dataIndex: 'last_price', align: 'right' },
  { title: '成本值', key: 'cost', dataIndex: 'cost', align: 'right' },
  {
    title: '盈亏',
    key: 'total_pnl',
    dataIndex: 'total_pnl',
    width: 128,
    align: 'right',
    sorter: true,
  },
  {
    title: '盈亏 %',
    key: 'total_pnl_percent',
    dataIndex: 'total_pnl_percent',
    width: 80,
    align: 'right',
    sorter: true,
    fixed: 'right',
  },
  { title: '操作', key: 'actions', width: 64, align: 'center', fixed: 'right' },
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
  symbol: 56,
  cycle_index: 54,
  status: 62,
  started_at: 132,
  ended_at: 132,
  trade_count: 72,
  cycle_count: 64,
  allocation: 72,
  quantity: 76,
  avg_price: 86,
  last_price: 116,
  stop_levels: 132,
  breakeven_percent: 72,
  position_limit: 96,
  price: 86,
  cost_basis: 92,
  cost: 92,
  market_value: 92,
  total_pnl: 110,
  total_pnl_percent: 72,
  actions: 56,
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

function stopPriceText(value) {
  return value == null ? '—' : formatMoney(value).replace(/ [A-Z]{3}$/, '')
}

function positionLimit(record) {
  const qty = toNumber(record?.quantity)
  const cost = toNumber(record?.lot_cost_basis ?? record?.cost_basis)
  const targetQty = record?.target_qty == null ? null : toNumber(record.target_qty)
  const maxBuy = record?.max_buy_amount == null ? null : toNumber(record.max_buy_amount)
  const set = targetQty != null || maxBuy != null
  const qtyOver = targetQty != null && qty > targetQty + 1e-6
  const amountOver = maxBuy != null && cost > maxBuy + 0.005
  const ok = set ? !qtyOver && !amountOver : null
  const parts = []
  if (targetQty != null) parts.push(`${formatQty(qty)}/${formatQty(targetQty)}股`)
  if (maxBuy != null) {
    parts.push(
      `${formatMoney(cost).replace(/ [A-Z]{3}$/, '')}/${formatMoney(maxBuy).replace(/ [A-Z]{3}$/, '')}`,
    )
  }
  return {
    set,
    ok,
    label: !set ? '未设置' : ok ? '合理' : '超限',
    tone: !set ? '' : ok ? 'pnl-up' : 'pnl-down',
    detail: parts.join(' · '),
    lines: parts,
  }
}

function isFullStop(record) {
  if (record?.stop_price == null && record?.stop_qty == null) return false
  if (record?.stop_qty == null) return true
  return toNumber(record.stop_qty) >= toNumber(record.quantity) - 1e-6
}

/** Percent the last price must rise to reach average cost. 0 when already at or above cost. */
function breakevenPercent(record) {
  const avg = record?.avg_price == null ? NaN : toNumber(record.avg_price)
  const last = record?.last_price == null ? NaN : toNumber(record.last_price)
  if (!Number.isFinite(avg) || !Number.isFinite(last) || avg <= 0 || last <= 0) return null
  if (last + 1e-9 >= avg) return 0
  return (avg / last - 1) * 100
}

function breakevenText(value) {
  if (value == null) return '—'
  return `${Number(value).toFixed(2)}%`
}

function holdingStopMark(record) {
  if (record?.stop_price == null && record?.stop_qty == null) return null
  return {
    price:
      record.stop_price == null
        ? '—'
        : `${formatMoney(record.stop_price).replace(/ [A-Z]{3}$/, '')}$`,
    qty: `${isFullStop(record) ? 'ALL' : formatQty(record.stop_qty)}股`,
  }
}

const stopModalOpen = ref(false)
const stopModalRecord = ref(null)
const stopModalSaving = ref(false)
const stopForm = reactive({
  stopPrice: null,
  stopQty: null,
})

const adviceOpen = ref(false)
const adviceLoading = ref(false)
const indicatorBook = ref(null)
const selectedAdvice = ref(null)
const priceMetricKeys = [
  'volume',
  'turnover',
  'illiq',
  'anchor_date',
  'anchor_vwap',
  'vs_anchor',
  'ma20',
  'bandwidth',
  'vol',
  'mom',
  'vol_cap',
  'rp_weight',
  'pe',
  'erp',
]

function adviceNum(value, digits = 2) {
  if (value == null || value === '') return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function advicePct(value) {
  if (value == null || value === '') return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  return `${n.toFixed(1)}%`
}

function adviceSignedPct(value) {
  if (value == null || value === '') return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  return `${n > 0 ? '+' : ''}${n.toFixed(1)}%`
}

async function loadIndicators() {
  if (!isPricePage.value && subTab.value !== 'price') return
  adviceLoading.value = true
  try {
    indicatorBook.value = await fetchHoldingsAdvice()
  } catch {
    indicatorBook.value = null
  } finally {
    adviceLoading.value = false
  }
}

function metricsOf(symbol) {
  return (indicatorBook.value?.items || []).find((row) => row.symbol === symbol)?.metrics || null
}

function formatCount(value) {
  if (value == null || value === '') return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  const abs = Math.abs(n)
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(n / 1e4).toFixed(1)}万`
  return formatQty(n)
}

function moneyNumber(value, currency) {
  if (value == null || value === '') return null
  return formatMoney(value, currency).replace(/ [A-Z]{3}$/, '')
}

function priceMetricText(record, key) {
  const metrics = metricsOf(record.symbol)
  if (!metrics) return adviceLoading.value ? '…' : '—'
  const value = metrics[key]
  if (key === 'volume' || key === 'turnover') return formatCount(value)
  if (key === 'anchor_date') return value || '—'
  if (key === 'illiq') return adviceNum(value, 3)
  if (key === 'vs_anchor' || key === 'erp') return adviceSignedPct(value)
  if (key === 'bandwidth' || key === 'vol' || key === 'vol_cap' || key === 'rp_weight') return advicePct(value)
  return adviceNum(value)
}

function priceMetricTone(record, key) {
  const metrics = metricsOf(record.symbol)
  if (!metrics || metrics[key] == null) return ''
  if (key === 'vs_anchor' || key === 'erp' || key === 'mom') return pnlClass(metrics[key])
  return ''
}

function openStockAdvice(record) {
  const item = (indicatorBook.value?.items || []).find((row) => row.symbol === record.symbol)
  if (!item) return
  selectedAdvice.value = item
  adviceOpen.value = true
}

function priceRow(record) {
  return { onClick: () => openStockAdvice(record) }
}

const sentiment = ref(null)
const sentimentState = ref('idle')
const sentimentChartRef = ref(null)
let sentimentChart

const trendOpen = ref(false)
const trendLoading = ref(false)
const trendSymbol = ref('')
const trendBook = ref(null)
const trendChartRef = ref(null)
let trendChart
let trendLoadToken = 0

const trendColumns = [
  { title: '日期', dataIndex: 'date', key: 'date', width: 108, fixed: 'left' },
  { title: '收盘', dataIndex: 'close', key: 'close', align: 'right', width: 80 },
  { title: '成交量', key: 'volume', align: 'right', width: 100 },
  { title: '成交额', key: 'turnover', align: 'right', width: 100 },
  { title: 'Amihud', key: 'illiq', align: 'right', width: 96 },
  { title: 'MA20', key: 'ma20', align: 'right', width: 92 },
  { title: '距均线', key: 'vs_ma20', align: 'right', width: 92 },
  { title: 'AVWAP', key: 'anchor_vwap', align: 'right', width: 100 },
  { title: '距锚', key: 'vs_anchor', align: 'right', width: 84 },
  { title: '带宽', key: 'bandwidth', align: 'right', width: 84 },
  { title: '波动', key: 'vol', align: 'right', width: 84 },
  { title: '动量', key: 'mom', align: 'right', width: 80 },
]

const trendCursor = ref(null)
const trendRows = computed(() => [...(trendBook.value?.points || [])].reverse())
const trendLatest = computed(() => {
  const points = trendBook.value?.points || []
  return points.length ? points[points.length - 1] : null
})
const indicatorHints = {
  volume: '已收盘交易日的成交股数。低于 20 日均量是锁仓的条件之一；超过均量 2 倍是派发的条件之一。',
  turnover: '收盘价乘以成交量。Amihud 把它换成亿美元后再除。',
  illiq: '当日涨跌幅绝对值（百分点）除以成交额（亿美元）。涨幅超过 3%、成交量低于 20 日均量、且大于 1，标锁仓。成交量超过均量 2 倍、涨跌落在 -1% 到 +1.5%、且小于 0.1，标派发。',
  anchor_date: '近 20 个交易日最低价所在的日期。防守线从这一天起向后累积。',
  anchor_vwap: '从锚定日起，用 (高+低+收)/3 按成交量加权。图上是绿色虚线。',
  vs_anchor: '价格相对 AVWAP 的偏离。大于等于 0 为机构持仓安全，小于 0 为机构防守线失守。',
  ma20: '近 20 个交易日收盘价的平均数，也是布林带中轨。',
  vs_ma20: '收盘价相对 MA20 的偏离。',
  bandwidth: '(上轨 − 下轨) / 中轨。上下轨是 MA20 加减 2 倍样本标准差。创近 120 个交易日新低时标变盘蓄势。',
  vol: '近 20 日收益率的样本标准差乘以 √252，得到实际年化波动。',
  mom: '近 20 日累计涨跌幅除以年化波动。大于 0 为顺势，图底淡绿；小于 0 为左侧博弈，图底淡黄。',
  vol_cap: '目标波动 20% 除以年化波动，作为这只股票的仓位上限。',
  rp_weight: '多头之间按波动的倒数分配。对冲腿不参与，这里留空。',
  pe: '行情里的滚动市盈率，并且要和价格除以每股收益对得上才采用。',
  erp: '1/PE 减去美国 10 年期国债收益率。PE 缺失或不是正数时留空。',
}

const trendMarks = computed(() => {
  const last = trendLatest.value
  if (!last) return []
  const marks = []
  if (last.vs_anchor != null) {
    marks.push(
      last.vs_anchor >= 0
        ? { text: '机构持仓安全', tone: 'up' }
        : { text: '机构防守线失守', tone: 'down' },
    )
  }
  if (last.mom != null && last.mom > 0) marks.push({ text: '顺势', tone: 'up' })
  else if (last.mom != null && last.mom < 0) marks.push({ text: '左侧博弈', tone: 'warn' })
  if (last.vol_squeeze) marks.push({ text: '变盘蓄势', tone: 'info' })
  return marks
})

function disposeSentimentChart() {
  if (!sentimentChart) return
  sentimentChart.dispose()
  sentimentChart = null
}

function disposeTrendChart() {
  if (!trendChart) return
  trendChart.dispose()
  trendChart = null
}

async function loadSentiment() {
  if (!isPricePage.value && subTab.value !== 'price') return
  sentimentState.value = 'loading'
  try {
    sentiment.value = await fetchMarketSentiment()
    sentimentState.value = 'ready'
    await nextTick()
    renderSentimentChart()
  } catch {
    sentiment.value = null
    sentimentState.value = 'error'
  }
}

function chartSlider(dates, extra = {}) {
  return {
    type: 'slider',
    height: 18,
    bottom: 2,
    backgroundColor: '#f6f8fb',
    borderColor: 'transparent',
    fillerColor: 'rgba(20, 151, 125, 0.14)',
    handleSize: 14,
    handleStyle: { color: '#fff', borderColor: '#d5deea', borderWidth: 1 },
    moveHandleSize: 0,
    brushSelect: false,
    showDetail: false,
    dataBackground: {
      lineStyle: { color: '#b7c6da', width: 1 },
      areaStyle: { color: 'rgba(20, 151, 125, 0.08)' },
    },
    selectedDataBackground: {
      lineStyle: { color: '#14977d', width: 1 },
      areaStyle: { color: 'rgba(20, 151, 125, 0.16)' },
    },
    textStyle: { color: '#8b95a8', fontSize: 10 },
    labelFormatter: (value) => (typeof value === 'string' ? value.slice(5) : dates[value]?.slice(5) || ''),
    ...extra,
  }
}

function renderSentimentChart() {
  const book = sentiment.value
  if (!sentimentChartRef.value || !book) return
  const indexes = book.indexes || []
  const primary = indexes.find((item) => item.name === '纳斯达克') || indexes.find((item) => item.history?.length)
  const points = primary?.history?.length ? primary.history : book.vix?.history || []
  if (!points.length) return
  const primaryName = primary?.name === '纳斯达克' ? '纳指' : primary?.name || '指数'
  let secondaryName = ''
  let secondaryData = []
  if (book.vix?.history?.length) {
    secondaryName = 'VIX'
    const vixByDate = Object.fromEntries(book.vix.history.map((point) => [point.date, point.close]))
    secondaryData = points.map((point) => vixByDate[point.date] ?? null)
  } else {
    const other = indexes.find((item) => item !== primary && item.history?.length)
    if (other) {
      secondaryName = other.name
      const byDate = Object.fromEntries(other.history.map((point) => [point.date, point.close]))
      secondaryData = points.map((point) => byDate[point.date] ?? null)
    }
  }
  if (!sentimentChart) sentimentChart = echarts.init(sentimentChartRef.value)
  const dates = points.map((point) => point.date)
  const vixByDate = Object.fromEntries((book.vix?.history || []).map((point) => [point.date, point.close]))
  const zoomStart = dates[Math.max(0, dates.length - 60)]
  const zoomEnd = dates[dates.length - 1]
  const axisLabel = { color: '#8b95a8', fontSize: 11 }
  const series = [
    {
      name: primaryName,
      type: 'line',
      smooth: 0.18,
      showSymbol: false,
      symbol: 'circle',
      symbolSize: 7,
      data: points.map((point) => point.close),
      lineStyle: { width: 2.2, color: '#14977d', cap: 'round', join: 'round' },
      itemStyle: { color: '#14977d', borderColor: '#fff', borderWidth: 2 },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(20, 151, 125, 0.22)' },
          { offset: 1, color: 'rgba(20, 151, 125, 0.02)' },
        ]),
      },
      emphasis: { focus: 'series' },
    },
  ]
  if (secondaryName) {
    series.push({
      name: secondaryName,
      type: 'line',
      yAxisIndex: 1,
      smooth: 0.18,
      showSymbol: false,
      symbol: 'circle',
      symbolSize: 7,
      data: secondaryData,
      lineStyle: { width: 1.6, color: '#d9852a', cap: 'round', join: 'round' },
      itemStyle: { color: '#d9852a', borderColor: '#fff', borderWidth: 2 },
      emphasis: { focus: 'series' },
    })
  }
  sentimentChart.setOption(
    {
      animationDuration: 280,
      grid: { left: 4, right: 4, top: 32, bottom: 28, containLabel: true },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(255,255,255,0.96)',
        borderColor: '#e7ebf2',
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: '#243047', fontSize: 12 },
        axisPointer: { type: 'line', lineStyle: { color: '#c5d0e0', type: 'dashed', width: 1 } },
      },
      legend: {
        top: 0,
        right: 0,
        icon: 'circle',
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 14,
        textStyle: { color: '#7b8598', fontSize: 12 },
      },
      dataZoom: [
        {
          type: 'inside',
          zoomOnMouseWheel: true,
          moveOnMouseMove: true,
          moveOnMouseWheel: false,
          startValue: zoomStart,
          endValue: zoomEnd,
        },
        chartSlider(dates, { startValue: zoomStart, endValue: zoomEnd }),
      ],
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: '#e7ebf2' } },
        axisTick: { show: false },
        axisLabel: {
          ...axisLabel,
          hideOverlap: true,
          formatter: (value) => String(value).slice(5),
        },
      },
      yAxis: [
        {
          type: 'value',
          scale: true,
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: '#eef2f6' } },
          axisLabel,
        },
        {
          type: 'value',
          scale: true,
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel,
        },
      ],
      series,
    },
    true,
  )
  sentimentChart.resize()
}

function openTrend() {
  trendOpen.value = true
  if (!trendSymbol.value && data.value.items[0]) {
    trendSymbol.value = data.value.items[0].symbol
    return
  }
  loadTrend()
}

function trendPointChange(point) {
  const points = trendBook.value?.points || []
  const index = points.findIndex((item) => item.date === point?.date)
  const prev = index > 0 ? points[index - 1]?.close : null
  if (!point?.close || !prev) return null
  return (point.close / prev - 1) * 100
}

function captureTrendRange(dates) {
  if (!trendChart || !dates.length) return null
  const zoom = trendChart.getOption()?.dataZoom?.[0]
  if (!zoom) return null
  if (typeof zoom.startValue === 'string' && typeof zoom.endValue === 'string') {
    return { startValue: zoom.startValue, endValue: zoom.endValue }
  }
  const start = Math.round(((zoom.start ?? 0) / 100) * (dates.length - 1))
  const end = Math.round(((zoom.end ?? 100) / 100) * (dates.length - 1))
  return { startValue: dates[Math.max(0, start)], endValue: dates[Math.min(dates.length - 1, end)] }
}

async function loadTrend() {
  const symbol = trendSymbol.value
  if (!symbol) return
  const token = ++trendLoadToken
  trendLoading.value = true
  trendCursor.value = null
  try {
    let before
    let first = true
    while (trendOpen.value && token === trendLoadToken) {
      const page = await fetchIndicatorHistory(symbol, before ? { before } : {})
      if (token !== trendLoadToken || trendSymbol.value !== symbol || !trendOpen.value) return
      const extend = !first
      if (first) {
        trendBook.value = page
        trendCursor.value = page.points?.[page.points.length - 1] || null
        first = false
        trendLoading.value = false
      } else if (page.points?.length) {
        const prev = trendBook.value
        trendBook.value = {
          ...prev,
          points: [...page.points, ...(prev?.points || [])],
          has_more: page.has_more,
          before: page.before,
        }
      }
      await nextTick()
      renderTrendChart({ keepRange: extend })
      if (!page.has_more || !page.before || page.before === before) break
      before = page.before
    }
  } catch {
    if (token === trendLoadToken) trendBook.value = null
  } finally {
    if (token === trendLoadToken) trendLoading.value = false
  }
}

function renderTrendChart({ keepRange = false } = {}) {
  const points = trendBook.value?.points || []
  if (!trendChartRef.value || !points.length) return
  if (!trendChart) trendChart = echarts.init(trendChartRef.value)
  const previousDates = trendChart.getOption()?.xAxis?.[0]?.data || []
  const kept = keepRange ? captureTrendRange(previousDates) : null
  const dates = points.map((point) => point.date)
  const upColor = '#089981'
  const downColor = '#f23645'
  const candles = points.map((point) => {
    const close = point.close
    const open = point.open ?? close
    const low = point.low ?? Math.min(open ?? close, close)
    const high = point.high ?? Math.max(open ?? close, close)
    return [open, close, low, high]
  })
  const marks = []
  points.forEach((point, index) => {
    if (point.signal === 'squeeze') {
      marks.push({
        coord: [dates[index], point.low],
        symbol: 'arrow',
        symbolRotate: 0,
        symbolOffset: [0, 10],
        itemStyle: { color: upColor },
        label: { formatter: '锁仓', position: 'bottom', color: upColor, fontSize: 10 },
      })
    } else if (point.signal === 'distribute') {
      marks.push({
        coord: [dates[index], point.high],
        symbol: 'arrow',
        symbolRotate: 180,
        symbolOffset: [0, -10],
        itemStyle: { color: '#e6a700' },
        label: { formatter: '派发', position: 'top', color: '#8a6400', fontSize: 10 },
      })
    }
  })
  const mom = points[points.length - 1]?.mom
  const wash = mom > 0 ? 'rgba(8,153,129,0.08)' : mom < 0 ? 'rgba(230,167,0,0.10)' : 'transparent'
  const zoomEnd = dates[dates.length - 1]
  const zoomStart = kept?.startValue && dates.includes(kept.startValue)
    ? kept.startValue
    : dates[Math.max(0, dates.length - 60)]
  const zoomStop = kept?.endValue && dates.includes(kept.endValue) ? kept.endValue : zoomEnd
  trendChart.setOption(
    {
      animationDuration: 280,
      axisPointer: {
        link: [{ xAxisIndex: [0, 1, 2] }],
        lineStyle: { color: '#c5d0e0', type: 'dashed', width: 1 },
      },
      legend: {
        top: 0,
        right: 4,
        icon: 'circle',
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 12,
        textStyle: { fontSize: 12, color: '#7b8598' },
      },
      tooltip: {
        show: true,
        showContent: false,
        trigger: 'axis',
        axisPointer: { type: 'cross', lineStyle: { color: '#c5d0e0', type: 'dashed', width: 1 } },
      },
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: [0, 1, 2],
          zoomOnMouseWheel: true,
          moveOnMouseMove: false,
          moveOnMouseWheel: false,
          preventDefaultMouseMove: false,
          startValue: zoomStart,
          endValue: zoomStop,
        },
        chartSlider(dates, {
          xAxisIndex: [0, 1, 2],
          left: 28,
          right: 4,
          startValue: zoomStart,
          endValue: zoomStop,
        }),
      ],
      grid: [
        { left: 28, right: 4, top: 28, bottom: '30%' },
        { left: 28, right: 4, top: '73%', height: '8%' },
        { left: 28, right: 4, top: '83%', height: '8%' },
      ],
      xAxis: [0, 1, 2].map((gridIndex) => ({
        type: 'category',
        gridIndex,
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: '#e7ebf2' } },
        axisLabel: {
          show: gridIndex === 2,
          color: '#8b95a8',
          fontSize: 11,
          hideOverlap: true,
          formatter: (value) => String(value).slice(5),
        },
        axisTick: { show: false },
        splitLine: { show: false },
      })),
      yAxis: [
        {
          scale: true,
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: '#eef2f6' } },
          axisLabel: { color: '#8b95a8', fontSize: 11, margin: 4, align: 'right' },
        },
        { gridIndex: 1, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
        { gridIndex: 2, scale: true, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: candles,
          itemStyle: {
            color: upColor,
            color0: downColor,
            borderColor: upColor,
            borderColor0: downColor,
          },
          markPoint: { symbolSize: 10, data: marks },
          markArea: {
            silent: true,
            itemStyle: { color: wash },
            data: [[{ xAxis: dates[0] }, { xAxis: dates[dates.length - 1] }]],
          },
        },
        {
          name: 'MA20',
          type: 'line',
          showSymbol: false,
          smooth: 0.12,
          data: points.map((point) => point.ma20),
          lineStyle: { width: 1.8, color: '#e6a700', cap: 'round', join: 'round' },
          itemStyle: { color: '#e6a700' },
        },
        {
          name: 'AVWAP',
          type: 'line',
          showSymbol: false,
          data: points.map((point) => point.anchor_vwap),
          lineStyle: { width: 2, type: 'dashed', color: '#14977d', cap: 'round' },
          itemStyle: { color: '#14977d' },
        },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: points.map((point) => ({
            value: point.volume,
            itemStyle: {
              color: (point.close ?? 0) >= (point.open ?? point.close ?? 0) ? upColor : downColor,
            },
          })),
        },
        {
          name: 'Amihud',
          type: 'bar',
          xAxisIndex: 2,
          yAxisIndex: 2,
          data: points.map((point) => ({
            value: point.illiq,
            itemStyle: {
              color:
                point.signal === 'squeeze' ? upColor : point.signal === 'distribute' ? '#e6a700' : '#c5cad3',
            },
          })),
        },
      ],
    },
    true,
  )
  trendChart.off('updateAxisPointer')
  trendChart.on('updateAxisPointer', (event) => {
    const axis = (event?.axesInfo || []).find((item) => item.axisDim === 'x')
    const raw = axis?.value
    const date = typeof raw === 'number' ? points[raw]?.date : raw
    const point = points.find((item) => item.date === date)
    if (point) trendCursor.value = point
  })
  trendChart.off('globalout')
  trendChart.on('globalout', () => {
    trendCursor.value = points[points.length - 1] || null
  })
  trendChart.resize()
}

function trendCell(record, key) {
  const value = record[key]
  if (key === 'volume' || key === 'turnover') return formatCount(value)
  if (key === 'illiq') return adviceNum(value, 3)
  if (key === 'vs_ma20' || key === 'vs_anchor') return adviceSignedPct(value)
  if (key === 'bandwidth' || key === 'vol') return advicePct(value)
  if (key === 'mom') return adviceNum(value, 2)
  return adviceNum(value)
}

function trendTone(record, key) {
  if (!['vs_ma20', 'vs_anchor', 'mom'].includes(key) || record[key] == null) return ''
  return pnlClass(record[key])
}

const limitModalOpen = ref(false)
const limitModalRecord = ref(null)
const limitModalSaving = ref(false)
const limitForm = reactive({
  targetQty: null,
  maxBuyAmount: null,
})

function openStopModal(record) {
  stopModalRecord.value = record
  stopForm.stopPrice = record.stop_price == null ? null : toNumber(record.stop_price)
  const qty = record.stop_qty == null ? null : toNumber(record.stop_qty)
  stopForm.stopQty =
    qty != null && qty < toNumber(record.quantity) - 1e-6 ? qty : null
  stopModalOpen.value = true
}

function fillStopFrom(key) {
  const value = stopModalRecord.value?.[key]
  if (value != null) stopForm.stopPrice = Number(toNumber(value).toFixed(4))
}

const stopFormHasValue = computed(
  () => stopForm.stopPrice != null || stopForm.stopQty != null,
)

async function saveStopModal() {
  const record = stopModalRecord.value
  if (!record) return
  const stopPrice = stopForm.stopPrice == null ? null : Number(stopForm.stopPrice)
  let stopQty = stopForm.stopQty == null ? null : Number(stopForm.stopQty)
  if (stopQty != null && stopQty >= toNumber(record.quantity) - 1e-6) {
    stopQty = null
    stopForm.stopQty = null
  }
  stopModalSaving.value = true
  try {
    const saved = await updateHoldingStop({
      symbol: record.symbol,
      stop_price: stopPrice,
      stop_qty: stopQty,
    })
    record.stop_price = saved.stop_price
    record.stop_qty = saved.stop_qty
    record.take_profit_price = saved.take_profit_price
    applyLimitFields(record, saved)
    message.success(stopPrice == null && stopQty == null ? '已清除止损' : '止损已保存')
    stopModalOpen.value = false
  } catch (err) {
    return Promise.reject(err)
  } finally {
    stopModalSaving.value = false
  }
}

async function clearStopModal() {
  stopForm.stopPrice = null
  stopForm.stopQty = null
  await saveStopModal()
}

function confirmAdoptLevel(kind) {
  const brief = trendBook.value?.brief
  const symbol = trendSymbol.value
  const price = kind === 'stop' ? brief?.stop_price : brief?.target_price
  if (!symbol || price == null) return
  const label = kind === 'stop' ? '止损' : '止盈'
  const ticker = tickerOf(symbol)
  const shown = adviceNum(price)
  Modal.confirm({
    title: `把 ${ticker} 的${label}设为 ${shown}？`,
    content: `确认后写入这只股票的${label}。`,
    okText: '确认',
    cancelText: '取消',
    centered: true,
    async onOk() {
      const payload = { symbol }
      if (kind === 'stop') payload.stop_price = Number(price)
      else payload.take_profit_price = Number(price)
      const saved = await updateHoldingStop(payload)
      const row = data.value.items.find((item) => item.symbol === symbol)
      if (row) {
        row.stop_price = saved.stop_price
        row.stop_qty = saved.stop_qty
        row.take_profit_price = saved.take_profit_price
      }
      message.success(`已把 ${ticker} 的${label}设为 ${shown}`)
    },
  })
}

function openLimitModal(record) {
  limitModalRecord.value = record
  limitForm.targetQty = record.target_qty == null ? null : toNumber(record.target_qty)
  limitForm.maxBuyAmount = record.max_buy_amount == null ? null : toNumber(record.max_buy_amount)
  limitModalOpen.value = true
}

const limitFormHasValue = computed(
  () => limitForm.targetQty != null || limitForm.maxBuyAmount != null,
)

const limitModalPreview = computed(() => {
  const record = limitModalRecord.value
  if (!record) return { ok: null, label: '未设置' }
  return positionLimit({
    ...record,
    target_qty: limitForm.targetQty,
    max_buy_amount: limitForm.maxBuyAmount,
  })
})

function applyLimitFields(record, saved) {
  record.target_qty = saved.target_qty
  record.max_buy_amount = saved.max_buy_amount
}

async function saveLimitModal() {
  const record = limitModalRecord.value
  if (!record) return
  const targetQty = limitForm.targetQty == null ? null : Number(limitForm.targetQty)
  const maxBuyAmount = limitForm.maxBuyAmount == null ? null : Number(limitForm.maxBuyAmount)
  limitModalSaving.value = true
  try {
    const saved = await updateHoldingLimit({
      symbol: record.symbol,
      target_qty: targetQty,
      max_buy_amount: maxBuyAmount,
    })
    applyLimitFields(record, saved)
    message.success(targetQty == null && maxBuyAmount == null ? '已清除仓位设置' : '仓位设置已保存')
    limitModalOpen.value = false
  } catch (err) {
    return Promise.reject(err)
  } finally {
    limitModalSaving.value = false
  }
}

async function clearLimitModal() {
  limitForm.targetQty = null
  limitForm.maxBuyAmount = null
  await saveLimitModal()
}

function openAddPositionCalculator(record) {
  addPositionRecord.value = record
  addPositionForm.amount = null
  addPositionForm.targetPnlPct = null
  addPositionForm.stopPrice =
    toNumber(record.last_price) > 0 ? toNumber(record.last_price) : null
  addPositionOpen.value = true
}

function holdingSnapshot(record) {
  const qty = Math.max(toNumber(record.quantity), 0)
  const price = Math.max(toNumber(record.last_price), 0)
  const avg = toNumber(record.avg_price) || toNumber(record.lot_avg_price)
  const cost = toNumber(record.cost_basis) || qty * avg
  const value = price * qty
  const pnl = price > 0 ? value - cost : null
  const pnlPct = cost > 0 && pnl != null ? (pnl / cost) * 100 : null
  return { qty, price, avg, cost, value, pnl, pnlPct }
}

function applySignedAmount(snapshot, signedAmount) {
  const { qty, price, cost, value } = snapshot
  const amount = Number.isFinite(signedAmount) ? signedAmount : 0
  if (price <= 0 || amount === 0) {
    return {
      isSell: amount < 0,
      tradeAmount: 0,
      newQty: qty,
      totalCost: cost,
      exceedsPosition: false,
    }
  }
  if (amount > 0) {
    return {
      isSell: false,
      tradeAmount: amount,
      newQty: qty + amount / price,
      totalCost: cost + amount,
      exceedsPosition: false,
    }
  }
  const requested = Math.abs(amount)
  const tradeAmount = Math.min(requested, value)
  const newQty = Math.max(qty - tradeAmount / price, 0)
  return {
    isSell: true,
    tradeAmount,
    newQty,
    totalCost: newQty > 0 ? cost - tradeAmount : 0,
    exceedsPosition: requested > value + 0.005,
  }
}

const addPositionCalculation = computed(() => {
  const empty = {
    isSell: false,
    tradeAmount: 0,
    newQty: 0,
    newAvg: 0,
    totalCost: 0,
    currentPnl: null,
    currentPnlPct: null,
    afterPnl: null,
    afterPnlPct: null,
    stopPnl: null,
    stopPnlPct: null,
    exceedsPosition: false,
  }
  const record = addPositionRecord.value
  if (!record) return empty

  const snapshot = holdingSnapshot(record)
  const signedAmount =
    addPositionForm.amount == null ? 0 : toNumber(addPositionForm.amount)
  const traded = applySignedAmount(snapshot, signedAmount)
  const newAvg = traded.newQty > 0 ? traded.totalCost / traded.newQty : 0
  const afterPnl =
    snapshot.price > 0 && traded.newQty > 0
      ? snapshot.price * traded.newQty - traded.totalCost
      : traded.newQty === 0
        ? 0
        : null
  const afterPnlPct =
    traded.totalCost > 0 && afterPnl != null ? (afterPnl / traded.totalCost) * 100 : null
  const stop = toNumber(addPositionForm.stopPrice)
  const hasStop = addPositionForm.stopPrice != null && stop > 0 && traded.newQty > 0
  const stopPnl = hasStop ? stop * traded.newQty - traded.totalCost : null
  const stopPnlPct =
    hasStop && traded.totalCost > 0 && stopPnl != null
      ? (stopPnl / traded.totalCost) * 100
      : null

  return {
    isSell: traded.isSell,
    tradeAmount: traded.tradeAmount,
    newQty: traded.newQty,
    newAvg,
    totalCost: traded.totalCost,
    currentPnl: snapshot.pnl,
    currentPnlPct: snapshot.pnlPct,
    afterPnl,
    afterPnlPct,
    stopPnl,
    stopPnlPct,
    exceedsPosition: traded.exceedsPosition,
  }
})

const targetPnlPlan = computed(() => {
  const missing = { amount: null, side: null, reason: '', feasible: false }
  const record = addPositionRecord.value
  if (!record) return missing
  if (addPositionForm.targetPnlPct == null) {
    return { ...missing, reason: '输入目标盈亏比例后计算所需买卖金额' }
  }

  const snapshot = holdingSnapshot(record)
  const targetPct = toNumber(addPositionForm.targetPnlPct)
  if (snapshot.price <= 0 || snapshot.qty <= 0) {
    return { ...missing, reason: '当前持仓或价格无效' }
  }
  if (Math.abs(snapshot.pnl ?? 0) < 0.0001) {
    if (Math.abs(targetPct) < 0.0001) {
      return { amount: 0, side: null, reason: '当前已是 0%', feasible: true }
    }
    return { ...missing, reason: '当前持平，按现价买卖无法改变盈亏比例' }
  }
  if (Math.abs(targetPct) < 0.0001) {
    return { ...missing, reason: '按现价买卖无法把盈亏比例做到 0%' }
  }
  if ((snapshot.pnl > 0 && targetPct < 0) || (snapshot.pnl < 0 && targetPct > 0)) {
    return { ...missing, reason: '按现价买卖不能把亏损做成盈利，也不能把盈利做成亏损' }
  }

  const neededCost = (snapshot.pnl * 100) / targetPct
  if (!(neededCost > 0)) {
    return { ...missing, reason: '无法达到该盈亏比例' }
  }
  const amount = neededCost - snapshot.cost
  if (!Number.isFinite(amount)) {
    return { ...missing, reason: '无法达到该盈亏比例' }
  }
  if (Math.abs(amount) < 0.01) {
    return { amount: 0, side: null, reason: '当前已接近该盈亏比例', feasible: true }
  }
  if (amount > 0) {
    return {
      amount,
      side: 'Buy',
      reason: `需买入 ${formatMoney(amount)}`,
      feasible: true,
    }
  }
  if (Math.abs(amount) > snapshot.value + 0.005) {
    return { ...missing, reason: '即使全部卖出也无法达到该盈亏比例' }
  }
  const remainingQty = snapshot.qty - Math.abs(amount) / snapshot.price
  if (remainingQty <= 0.0001) {
    return { ...missing, reason: '达到该比例需要卖完全部持仓' }
  }
  return {
    amount,
    side: 'Sell',
    reason: `需卖出 ${formatMoney(Math.abs(amount))}`,
    feasible: true,
  }
})

const positionsSummary = computed(() => {
  // 当前持仓行合计；% = 盈亏合计 / 成本值合计。
  const items = data.value.items || []
  const sumKey = (key) => items.reduce((acc, row) => acc + toNumber(row[key]), 0)
  const pnl = sumKey('total_pnl')
  const costBasis = sumKey('cost_basis')
  const marketValue = sumKey('market_value')
  return {
    trade_count: sumKey('trade_count'),
    allocation: sumKey('allocation'),
    quantity: sumKey('quantity'),
    cost_basis: sumKey('cost_basis'),
    market_value: marketValue,
    total_pnl: pnl,
    total_pnl_percent: costBasis > 0 ? (pnl / costBasis) * 100 : 0,
    breakeven_percent:
      marketValue > 0 ? (costBasis > marketValue ? (costBasis / marketValue - 1) * 100 : 0) : null,
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
  { text: '—', align: 'right' }, // 止损价格按个股计算
  { text: '—', align: 'right' }, // 仓位按个股设置
  { text: formatMoney(positionsSummary.value.cost_basis), align: 'right' }, // 成本值
  { text: formatMoney(positionsSummary.value.market_value), align: 'right' }, // 值
  // 累计盈亏
  {
    text: formatMoney(positionsSummary.value.total_pnl),
    tone: pnlClass(positionsSummary.value.total_pnl),
    align: 'right',
  },
  {
    text: breakevenText(positionsSummary.value.breakeven_percent),
    tone: positionsSummary.value.breakeven_percent > 0 ? 'pnl-down' : '',
    align: 'right',
  },
  // 累计盈亏 %
  {
    text: formatPct(positionsSummary.value.total_pnl_percent),
    tone: pnlClass(positionsSummary.value.total_pnl_percent),
    align: 'right',
  },
  { text: '', align: 'center' }, // 操作
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
  { text: '', align: 'center' }, // 操作
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
const imagePrice = (v) => (isBlank(v) ? '—' : formatMoney(v).replace(/ [A-Z]{3}$/, ''))
const imagePct = (v) => (isBlank(v) ? '—' : formatPct(v))
const imageSymbol = (r) => ({ text: tickerOf(r.symbol), secondary: r.name || '' })
const imagePnl = (v) => ({ text: imageMoney(v), tone: pnlClass(v) })
const imagePriceCell = (v, pct) => {
  if (isBlank(v)) return { text: '—' }
  const suffix = isBlank(pct) ? '' : imagePct(pct)
  return {
    text: imagePrice(v),
    suffix,
    suffixTone: suffix ? pnlClass(pct) : '',
  }
}

const positionImageCells = {
  symbol: imageSymbol,
  trade_count: (r) => String(r.trade_count ?? 0),
  allocation: (r) => formatPct(r.allocation).replace('+', ''),
  quantity: (r) => formatQty(r.quantity),
  avg_price: (r) => imagePrice(r.avg_price),
  last_price: (r) => imagePriceCell(r.last_price, r.day_change_percent),
  stop_levels: (r) => {
    const mark = holdingStopMark(r)
    const parts = [
      `建议 ${imagePrice(r.suggested_stop_price)}`,
      `必须 ${imagePrice(r.mandatory_stop_price)}`,
    ]
    if (mark) parts.push(`止损 ${mark.price} ${mark.qty}`)
    if (r.take_profit_price != null) parts.push(`止盈 ${imagePrice(r.take_profit_price)}`)
    return { text: parts.join('  ') }
  },
  position_limit: (r) => {
    const limit = positionLimit(r)
    return { text: limit.detail ? `${limit.label} ${limit.detail}` : limit.label, tone: limit.tone }
  },
  cost_basis: (r) => imageMoney(r.cost_basis),
  market_value: (r) => imageMoney(r.market_value),
  total_pnl: (r) => imagePnl(r.total_pnl),
  total_pnl_percent: (r) => ({ text: imagePct(r.total_pnl_percent), tone: pnlClass(r.total_pnl_percent) }),
  breakeven_percent: (r) => {
    const value = breakevenPercent(r)
    return { text: breakevenText(value), tone: value > 0 ? 'pnl-down' : '' }
  },
  actions: () => '',
}

const cumulativeImageCells = {
  symbol: imageSymbol,
  cycle_count: (r) => String(r.cycle_count ?? 0),
  trade_count: (r) => String(r.trade_count ?? 0),
  avg_price: (r) => imagePrice(r.avg_price),
  last_price: (r) => imagePriceCell(r.last_price),
  cost: (r) => imageMoney(r.cost),
  total_pnl: (r) => imagePnl(r.total_pnl),
  total_pnl_percent: (r) => ({ text: imagePct(r.total_pnl_percent), tone: pnlClass(r.total_pnl_percent) }),
  actions: () => '',
}

const priceImageCells = {
  symbol: imageSymbol,
  last_price: (r) => imagePrice(r.last_price),
  day_change: (r) => (r.day_change == null ? '—' : imagePnl(r.day_change)),
  day_change_percent: (r) => ({
    text: r.day_change_percent == null ? '—' : imagePct(r.day_change_percent),
    tone: pnlClass(r.day_change_percent),
  }),
  open_price: (r) => imagePrice(r.open_price),
  high_price: (r) => imagePrice(r.high_price),
  low_price: (r) => imagePrice(r.low_price),
  prev_close: (r) => imagePrice(r.prev_close),
  volume: (r) => priceMetricText(r, 'volume'),
  turnover: (r) => priceMetricText(r, 'turnover'),
  illiq: (r) => priceMetricText(r, 'illiq'),
  anchor_date: (r) => priceMetricText(r, 'anchor_date'),
  anchor_vwap: (r) => priceMetricText(r, 'anchor_vwap'),
  vs_anchor: (r) => priceMetricText(r, 'vs_anchor'),
  ma20: (r) => priceMetricText(r, 'ma20'),
  bandwidth: (r) => priceMetricText(r, 'bandwidth'),
  vol: (r) => priceMetricText(r, 'vol'),
  mom: (r) => priceMetricText(r, 'mom'),
  vol_cap: (r) => priceMetricText(r, 'vol_cap'),
  rp_weight: (r) => priceMetricText(r, 'rp_weight'),
  pe: (r) => priceMetricText(r, 'pe'),
  erp: (r) => priceMetricText(r, 'erp'),
}

const priceImageSummary = computed(() => [])

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
  price: { title: '价格', columns: priceColumns, cells: priceImageCells, summary: priceImageSummary },
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

function onViewportResize() {
  viewportWidth.value = window.innerWidth
  priceChart?.resize()
  sentimentChart?.resize()
  trendChart?.resize()
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

watch(subTab, () => {
  if (isPricePage.value) return
  sortState.field = null
  sortState.order = null
  pagination.current = 1
  load()
})
watch(priceModalOpen, (open) => {
  if (!open) disposePriceChart()
})
watch(trendOpen, (open) => {
  if (!open) {
    trendLoadToken += 1
    disposeTrendChart()
  }
})
watch(trendSymbol, () => {
  if (trendOpen.value) loadTrend()
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
  disposeSentimentChart()
  disposeTrendChart()
})
</script>

<template>
  <div class="holdings-view" :class="{ 'price-page': isPricePage }">
    <a-tabs v-if="!isPricePage" v-model:activeKey="subTab" size="small">
      <a-tab-pane key="positions" tab="持仓" />
      <a-tab-pane key="cumulative" tab="累计" />
      <a-tab-pane key="closed" tab="清仓" />
    </a-tabs>

    <template v-if="!isPricePage && subTab === 'cumulative'">
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
        :expand-column-width="expandColumnWidth"
        expand-fixed="left"
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
                >{{ formatMoney(cycle.avg_price).replace(/ [A-Z]{3}$/, '') }}</a>
                <span v-else>—</span>
              </template>
              <template v-else-if="column.key === 'price'">
                {{ cycle.price == null ? '—' : formatMoney(cycle.price).replace(/ [A-Z]{3}$/, '') }}
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
          <template v-else-if="column.key === 'actions'">
            <div class="holding-row-actions">
              <a-button
                type="link"
                size="small"
                class="holding-action-link"
                @click.stop="openAddPositionCalculator(record)"
              >
                试算
              </a-button>
            </div>
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
            >{{ formatMoney(record.avg_price).replace(/ [A-Z]{3}$/, '') }}</a>
            <span v-else>—</span>
          </template>
          <template v-else-if="column.key === 'last_price'">
            <span v-if="record.last_price == null">—</span>
            <span v-else class="close-price-cell">
              {{ moneyNumber(record.last_price, record.currency) }}
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
        :expand-column-width="expandColumnWidth"
        expand-fixed="left"
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
      <header v-if="isPricePage" class="price-header">
        <div>
          <p class="price-eyebrow">行情 / 价格</p>
          <h1 class="price-title">价格<span class="price-title-dot"></span></h1>
          <p class="price-description">先看市场情绪，再看每只持仓的指标。点击一行打开这只股票的建议。</p>
        </div>
        <div class="price-header-actions">
          <a-button class="price-action" :disabled="!data.items.length" @click="openTrend">指标趋势</a-button>
          <a-button
            class="price-action"
            :loading="imageExporting"
            :disabled="!data.items.length"
            @click="handleDownloadImage('price')"
          >
            <DownloadOutlined /> 下载图片
          </a-button>
        </div>
      </header>
      <section v-if="isPricePage" class="price-hero">
        <div class="price-hero-copy">
          <p class="price-kicker"><span class="price-kicker-dot"></span>当前市场情绪</p>
          <h2 v-if="sentiment">{{ sentiment.label }}</h2>
          <h2 v-else-if="sentimentState !== 'error'">读取中</h2>
          <h2 v-else>暂时没有取到</h2>
          <p class="price-hero-note">
            {{ sentiment?.note || (sentimentState === 'error' ? '市场情绪暂时没有取到。' : '正在读取涨跌家数和指数。') }}
          </p>
          <div v-if="sentiment" class="price-hero-meta">
            <span v-if="sentiment.as_of">截至 {{ sentiment.as_of }}</span>
            <span v-if="sentiment.breadth">{{ sentiment.breadth.universe }} · {{ sentiment.breadth.count }} 只</span>
            <span v-if="sentiment.breadth">平盘 {{ sentiment.breadth.unchanged }}</span>
            <span v-if="sentiment.breadth?.strong_up != null">涨超 2% {{ sentiment.breadth.strong_up }}</span>
            <span v-if="sentiment.breadth?.strong_down != null">跌超 2% {{ sentiment.breadth.strong_down }}</span>
          </div>
        </div>
        <div v-if="sentiment?.breadth" class="price-metrics">
          <article>
            <span class="price-metric-icon teal"><RiseOutlined /></span>
            <div>
              <p>上涨</p>
              <strong class="pnl-up">{{ sentiment.breadth.advancers }}</strong>
              <span>当前行情里的上涨家数</span>
            </div>
          </article>
          <article>
            <span class="price-metric-icon amber"><FallOutlined /></span>
            <div>
              <p>下跌</p>
              <strong class="pnl-down">{{ sentiment.breadth.decliners }}</strong>
              <span>当前行情里的下跌家数</span>
            </div>
          </article>
          <article>
            <span class="price-metric-icon blue"><LineChartOutlined /></span>
            <div>
              <p>上涨占比</p>
              <strong>{{ sentiment.breadth.up_ratio == null ? '—' : `${sentiment.breadth.up_ratio.toFixed(1)}%` }}</strong>
              <span>{{ sentiment.breadth.universe }}里上涨所占的比例</span>
            </div>
          </article>
          <article v-if="sentiment.breadth.median_change != null">
            <span class="price-metric-icon violet"><DashboardOutlined /></span>
            <div>
              <p>中位涨跌</p>
              <strong :class="pnlClass(sentiment.breadth.median_change)">
                {{ `${sentiment.breadth.median_change > 0 ? '+' : ''}${sentiment.breadth.median_change.toFixed(2)}%` }}
              </strong>
              <span>{{ sentiment.breadth.universe }}的中位数</span>
            </div>
          </article>
        </div>
      </section>
      <section v-if="isPricePage && sentiment && (sentiment.vix || sentiment.indexes?.length)" class="price-tape">
        <article v-if="sentiment.vix">
          <p>VIX</p>
          <strong>{{ sentiment.vix.last == null ? '—' : sentiment.vix.last.toFixed(2) }}</strong>
          <span :class="pnlClass(sentiment.vix.day_change)">
            {{ sentiment.vix.day_change == null ? '—' : `${sentiment.vix.day_change > 0 ? '+' : ''}${sentiment.vix.day_change.toFixed(2)}%` }}
          </span>
        </article>
        <article v-for="item in sentiment.indexes" :key="item.name">
          <p>{{ item.name }}</p>
          <strong :class="pnlClass(item.day_change)">
            {{ item.day_change == null ? '—' : `${item.day_change > 0 ? '+' : ''}${item.day_change.toFixed(2)}%` }}
          </strong>
          <span>5日 {{ item.ret_5d == null ? '—' : `${item.ret_5d > 0 ? '+' : ''}${item.ret_5d.toFixed(2)}%` }}</span>
        </article>
        <div ref="sentimentChartRef" class="sentiment-chart"></div>
      </section>
      <div v-if="isPricePage" class="price-section-head">
        <h2 class="price-section-title">
          <span class="price-section-number">01</span>持仓行情
          <span class="price-section-badge">{{ data.items.length }} 只</span>
        </h2>
        <p class="price-section-sub">
          {{ adviceLoading ? '指标计算中' : indicatorBook?.as_of ? `指标截至 ${indicatorBook.as_of} 日线。` : '' }}
          点击一行看这只股票的建议。
        </p>
      </div>
      <div class="toolbar" :class="{ 'price-toolbar': isPricePage }">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
        <div v-if="!isPricePage" class="toolbar-actions">
          <a-button
            :loading="imageExporting"
            :disabled="!data.items.length"
            @click="handleDownloadImage('positions')"
          >
            <DownloadOutlined /> 下载图片
          </a-button>
        </div>
      </div>
      <div class="mobile-scroll-hint">{{ isPricePage ? '左右滑动查看成交量和指标' : '左右滑动查看价格、止损和盈亏' }}</div>
      <a-table
        class="mobile-data-table holdings-table positions-table"
        :class="{ 'price-table': isPricePage }"
        :custom-row="isPricePage ? priceRow : undefined"
        :columns="columns"
        :data-source="data.items"
        :loading="loading"
        row-key="symbol"
        :pagination="pagination"
        :scroll="tableScroll"
        size="middle"
        @change="onTableChange"
      >
        <template v-if="isPricePage" #headerCell="{ column }">
          <span class="col-head">
            <span>{{ column.title }}</span>
            <a-tooltip v-if="indicatorHints[column.key]" :title="indicatorHints[column.key]">
              <QuestionCircleOutlined class="col-hint" />
            </a-tooltip>
          </span>
        </template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'symbol'">
            <SymbolCell
              v-if="isPricePage"
              as-button
              :symbol="record.symbol"
              :name="record.name"
              :logo-url="record.logo_url"
              @pick="openStockAdvice(record)"
            />
            <SymbolCell v-else :symbol="record.symbol" :name="record.name" :logo-url="record.logo_url" />
          </template>
          <template v-else-if="priceMetricKeys.includes(column.key)">
            <span :class="priceMetricTone(record, column.key)">{{ priceMetricText(record, column.key) }}</span>
          </template>
          <template v-else-if="column.key === 'actions'">
            <div class="holding-row-actions">
              <a-button
                type="link"
                size="small"
                class="holding-action-link"
                @click.stop="openAddPositionCalculator(record)"
              >
                试算
              </a-button>
              <a-button
                type="link"
                size="small"
                class="holding-action-link"
                :class="{
                  'stop-danger': holdingStopHit(record, 'stop_price'),
                  'is-set': record.stop_price != null || record.stop_qty != null,
                }"
                :title="holdingStopMark(record)
                  ? `止损 ${holdingStopMark(record).price} · ${holdingStopMark(record).qty}`
                  : '设置止损'"
                @click.stop="openStopModal(record)"
              >
                止损
              </a-button>
            </div>
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
              {{ moneyNumber(record.last_price, record.currency) }}
              <span
                v-if="subTab === 'positions' && record.day_change_percent != null"
                class="price-day-change"
                :class="pnlClass(record.day_change_percent)"
                :title="record.day_change == null ? undefined : `今日 ${formatMoney(record.day_change)} · ${formatPct(record.day_change_percent)}`"
              >{{ formatPct(record.day_change_percent) }}</span>
            </span>
          </template>
          <template v-else-if="column.key === 'stop_levels'">
            <button
              type="button"
              class="holding-stop-stack"
              title="设置止损"
              @click.stop="openStopModal(record)"
            >
              <span class="holding-stop-tag is-suggest">
                建议 {{ stopPriceText(record.suggested_stop_price) }}
              </span>
              <span class="holding-stop-tag is-must">
                必须 {{ stopPriceText(record.mandatory_stop_price) }}
              </span>
              <span v-if="holdingStopMark(record)" class="holding-stop-tag is-set">
                止损 {{ holdingStopMark(record).price }} {{ holdingStopMark(record).qty }}
              </span>
              <span v-if="record.take_profit_price != null" class="holding-stop-tag is-target">
                止盈 {{ stopPriceText(record.take_profit_price) }}
              </span>
            </button>
          </template>
          <template v-else-if="column.key === 'position_limit'">
            <button
              type="button"
              class="holding-limit-stack"
              :class="positionLimit(record).tone"
              title="设置仓位"
              @click.stop="openLimitModal(record)"
            >
              <span class="holding-limit-status">{{ positionLimit(record).label }}</span>
              <span v-for="line in positionLimit(record).lines" :key="line" class="holding-limit-line">
                {{ line }}
              </span>
            </button>
          </template>
          <template v-else-if="column.key === 'avg_price'">
            <a
              v-if="record.avg_price != null && subTab === 'positions'"
              class="trade-count-link"
              :class="avgPriceClass(record)"
              @click.prevent="openAvgPriceChart(record)"
            >
              {{ formatMoney(record.avg_price).replace(/ [A-Z]{3}$/, '') }}
            </a>
            <span v-else>
              {{ record.avg_price == null ? '—' : formatMoney(record.avg_price).replace(/ [A-Z]{3}$/, '') }}
            </span>
          </template>
          <template v-else-if="['open_price', 'high_price', 'low_price', 'prev_close'].includes(column.key)">
            {{ record[column.key] == null ? '—' : moneyNumber(record[column.key], record.currency) }}
          </template>
          <template v-else-if="['cost_basis', 'market_value', 'day_change'].includes(column.key)">
            <span :class="column.key === 'day_change' ? pnlClass(record[column.key]) : ''">
              {{ record[column.key] == null ? '—' : formatMoney(record[column.key], record.currency || 'USD') }}
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
          <template v-else-if="column.key === 'breakeven_percent'">
            <span :class="breakevenPercent(record) > 0 ? 'pnl-down' : ''">
              {{ breakevenText(breakevenPercent(record)) }}
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
      v-model:open="adviceOpen"
      :title="selectedAdvice ? `${selectedAdvice.ticker} 建议` : '建议'"
      :footer="null"
      :width="viewportWidth > 560 ? 640 : '100%'"
      wrap-class-name="advice-modal"
      :centered="viewportWidth > 560"
    >
      <div v-if="selectedAdvice" class="advice-body">
        <p class="advice-headline">
          <span class="advice-stage" :class="`is-${selectedAdvice.tone}`">{{ selectedAdvice.stage }}</span>
          <span v-if="selectedAdvice.name" class="advice-name">{{ selectedAdvice.name }}</span>
        </p>
        <p v-if="selectedAdvice.headline" class="trend-brief-lead">{{ selectedAdvice.headline }}</p>
        <div v-if="selectedAdvice.stop_price != null" class="trend-levels">
          <div class="is-stop">
            <span>{{ selectedAdvice.stop_hit ? '已破止损' : '止损' }}</span>
            <strong>{{ adviceNum(selectedAdvice.stop_price) }}</strong>
          </div>
        </div>
        <div v-if="selectedAdvice.sections?.length" class="trend-brief advice-sections">
          <div class="trend-brief-grid">
            <article v-for="section in selectedAdvice.sections" :key="section.title">
              <h3>{{ section.title }}</h3>
              <p>{{ section.trend }}</p>
              <p class="trend-brief-advice">{{ section.advice }}</p>
            </article>
          </div>
        </div>
        <template v-else>
          <p v-for="(line, i) in selectedAdvice.lines" :key="i">{{ line }}</p>
        </template>
      </div>
    </a-modal>

    <a-modal
      v-model:open="trendOpen"
      title="指标趋势"
      :footer="null"
      :width="viewportWidth > 560 ? 980 : '100%'"
      wrap-class-name="advice-modal"
      :centered="viewportWidth > 560"
      destroy-on-close
      @after-open-change="(open) => open && renderTrendChart()"
    >
      <div class="trend-toolbar">
        <a-select
          v-model:value="trendSymbol"
          show-search
          option-filter-prop="label"
          :options="data.items.map((item) => ({ value: item.symbol, label: item.name || tickerOf(item.symbol) }))"
        />
      </div>
      <div v-if="trendLatest" class="trend-marks">
        <span v-for="mark in trendMarks" :key="mark.text" class="trend-mark" :class="`is-${mark.tone}`">
          {{ mark.text }}
        </span>
        <span v-if="trendLatest.anchor_date" class="sentiment-note">
          防守线从 {{ trendLatest.anchor_date }} 低点锚定
        </span>
      </div>
      <div v-if="trendBook?.brief" class="trend-brief">
        <p class="trend-brief-lead">{{ trendBook.brief.headline }}</p>
        <div class="trend-brief-grid">
          <article v-for="section in trendBook.brief.sections" :key="section.title">
            <h3>{{ section.title }}</h3>
            <p>{{ section.trend }}</p>
            <p class="trend-brief-advice">{{ section.advice }}</p>
          </article>
        </div>
        <p class="sentiment-note">{{ trendBook.brief.level_note }}</p>
      </div>
      <div v-if="trendBook?.brief && (trendBook.brief.stop_price != null || trendBook.brief.target_price != null)" class="trend-levels">
          <button
            v-if="trendBook.brief.stop_price != null"
            type="button"
            class="is-stop"
            title="点击后写入这只股票的止损"
            @click="confirmAdoptLevel('stop')"
          >
            <span>{{ trendBook.brief.stop_hit ? '已破止损' : '止损' }}</span>
            <strong>{{ adviceNum(trendBook.brief.stop_price) }}</strong>
          </button>
          <button
            v-if="trendBook.brief.target_price != null"
            type="button"
            class="is-target"
            title="点击后写入这只股票的止盈"
            @click="confirmAdoptLevel('target')"
          >
            <span>止盈</span>
            <strong>{{ adviceNum(trendBook.brief.target_price) }}</strong>
          </button>
        </div>
      <div v-if="trendCursor" class="trend-quote">
        <div class="trend-quote-head">
          <span class="trend-quote-date">{{ trendCursor.date }}</span>
          <span class="trend-quote-last">
            <b :class="pnlClass(trendPointChange(trendCursor))">{{ adviceNum(trendCursor.close) }}</b>
            <b class="trend-quote-change" :class="pnlClass(trendPointChange(trendCursor))">{{ trendPointChange(trendCursor) == null ? '' : formatPct(trendPointChange(trendCursor)) }}</b>
          </span>
        </div>
        <div class="trend-quote-grid">
          <span><em>开</em><b>{{ adviceNum(trendCursor.open) }}</b></span>
          <span><em>高</em><b>{{ adviceNum(trendCursor.high) }}</b></span>
          <span><em>低</em><b>{{ adviceNum(trendCursor.low) }}</b></span>
          <span><em>量</em><b>{{ formatCount(trendCursor.volume) }}</b></span>
          <span class="is-ma"><em>MA20</em><b>{{ adviceNum(trendCursor.ma20) }}</b></span>
          <span class="is-vwap"><em>AVWAP</em><b>{{ adviceNum(trendCursor.anchor_vwap) }}</b></span>
          <span><em>Amihud</em><b>{{ adviceNum(trendCursor.illiq, 3) }}</b></span>
        </div>
      </div>
      <div ref="trendChartRef" class="trend-chart"></div>
      <a-table
        class="trend-table"
        :columns="trendColumns"
        :data-source="trendRows"
        :loading="trendLoading"
        row-key="date"
        size="small"
        :pagination="false"
        :scroll="{ x: 1100, y: 280 }"
      >
        <template #headerCell="{ column }">
          <span class="col-head">
            <span>{{ column.title }}</span>
            <a-tooltip v-if="indicatorHints[column.key]" :title="indicatorHints[column.key]">
              <QuestionCircleOutlined class="col-hint" />
            </a-tooltip>
          </span>
        </template>
        <template #bodyCell="{ column, record }">
          <span v-if="column.key === 'date'">{{ record.date }}</span>
          <span v-else :class="trendTone(record, column.key)">{{ trendCell(record, column.key) }}</span>
        </template>
      </a-table>
    </a-modal>

    <a-modal
      v-model:open="addPositionOpen"
      :title="`${tickerOf(addPositionRecord?.symbol)} 试算`"
      :footer="null"
      width="560px"
      wrap-class-name="mobile-sheet-modal holdings-add-position-modal"
      :centered="viewportWidth > 560"
      destroy-on-close
    >
      <div v-if="addPositionRecord" class="add-position-calculator">
        <div class="calculator-overview">
          <div>
            <span>当前数量</span>
            <strong>{{ formatQty(addPositionRecord.quantity) }}</strong>
          </div>
          <div>
            <span>当前均价</span>
            <strong>{{ formatMoney(addPositionRecord.avg_price).replace(/ [A-Z]{3}$/, '') }}</strong>
          </div>
          <div>
            <span>当前价格</span>
            <strong>{{ addPositionRecord.last_price == null ? '—' : formatMoney(addPositionRecord.last_price).replace(/ [A-Z]{3}$/, '') }}</strong>
          </div>
          <div>
            <span>当前盈亏</span>
            <strong :class="pnlClass(addPositionCalculation.currentPnl)">
              {{ addPositionCalculation.currentPnl == null ? '—' : formatMoney(addPositionCalculation.currentPnl).replace(/ [A-Z]{3}$/, '') }}
            </strong>
            <small v-if="addPositionCalculation.currentPnlPct != null" :class="pnlClass(addPositionCalculation.currentPnlPct)">
              {{ formatPct(addPositionCalculation.currentPnlPct) }}
            </small>
          </div>
        </div>

        <div class="calculator-form-grid">
          <label class="calculator-field">
            <span>买卖金额（正买负卖）</span>
            <a-input-number
              v-model:value="addPositionForm.amount"
              :precision="2"
              :step="100"
              :controls="viewportWidth > 560"
              inputmode="decimal"
              placeholder="例如 1000 或 -500"
              style="width: 100%"
            />
          </label>
          <label class="calculator-field">
            <span>目标盈亏比例</span>
            <a-input-number
              v-model:value="addPositionForm.targetPnlPct"
              :precision="2"
              :step="1"
              :controls="viewportWidth > 560"
              inputmode="decimal"
              addon-after="%"
              placeholder="例如 10 或 -5"
              style="width: 100%"
            />
            <small :class="{ feasible: targetPnlPlan.feasible }">{{ targetPnlPlan.reason }}</small>
          </label>
          <label class="calculator-field">
            <span>止损价（默认当前价）</span>
            <a-input-number
              v-model:value="addPositionForm.stopPrice"
              :min="0.0001"
              :precision="4"
              :step="0.01"
              :controls="viewportWidth > 560"
              inputmode="decimal"
              placeholder="止损价格"
              style="width: 100%"
            />
          </label>
        </div>

        <div class="calculator-result-grid">
          <div class="calculator-result">
            <span>剩余数量</span>
            <strong>{{ formatQty(addPositionCalculation.newQty) }}</strong>
          </div>
          <div class="calculator-result">
            <span>剩余均价</span>
            <strong>{{ addPositionCalculation.newQty > 0 ? formatMoney(addPositionCalculation.newAvg).replace(/ [A-Z]{3}$/, '') : '—' }}</strong>
          </div>
          <div class="calculator-result">
            <span>盈亏比例</span>
            <strong :class="pnlClass(addPositionCalculation.afterPnlPct ?? addPositionCalculation.afterPnl)">
              {{ addPositionCalculation.afterPnlPct == null ? '—' : formatPct(addPositionCalculation.afterPnlPct) }}
            </strong>
            <small v-if="addPositionCalculation.afterPnl != null" :class="pnlClass(addPositionCalculation.afterPnl)">
              {{ formatMoney(addPositionCalculation.afterPnl).replace(/ [A-Z]{3}$/, '') }}
            </small>
            <small v-else-if="addPositionCalculation.isSell && addPositionCalculation.newQty === 0">
              已按现价全部卖出
            </small>
          </div>
          <div class="calculator-result">
            <span>止损亏损</span>
            <strong :class="pnlClass(addPositionCalculation.stopPnl)">
              {{ addPositionCalculation.stopPnl == null ? '—' : formatMoney(addPositionCalculation.stopPnl).replace(/ [A-Z]{3}$/, '') }}
            </strong>
            <small v-if="addPositionCalculation.stopPnlPct != null" :class="pnlClass(addPositionCalculation.stopPnlPct)">
              {{ formatPct(addPositionCalculation.stopPnlPct) }}
            </small>
          </div>
        </div>

        <a-alert
          v-if="addPositionCalculation.exceedsPosition"
          type="warning"
          show-icon
          message="卖出金额超过当前持仓市值，已按全部持仓试算。"
        />
        <p class="calculator-note">正数为买入、负数为卖出，按当前价试算。卖出亏损留在剩余持仓。这里仅作测算。</p>
      </div>
    </a-modal>

    <a-modal
      v-model:open="stopModalOpen"
      :title="`${tickerOf(stopModalRecord?.symbol)} 止损`"
      width="440px"
      wrap-class-name="mobile-sheet-modal holdings-stop-modal"
      centered
      destroy-on-close
      :confirm-loading="stopModalSaving"
      ok-text="保存"
      @ok="saveStopModal"
    >
      <div v-if="stopModalRecord" class="stop-modal">
        <div class="calculator-overview">
          <div>
            <span>当前数量</span>
            <strong>{{ formatQty(stopModalRecord.quantity) }}</strong>
          </div>
          <div>
            <span>当前均价</span>
            <strong>{{ formatMoney(stopModalRecord.avg_price).replace(/ [A-Z]{3}$/, '') }}</strong>
          </div>
          <div>
            <span>当前价格</span>
            <strong>{{ stopModalRecord.last_price == null ? '—' : formatMoney(stopModalRecord.last_price).replace(/ [A-Z]{3}$/, '') }}</strong>
          </div>
        </div>
        <div class="stop-modal-form">
          <label class="calculator-field">
            <span>止损价格</span>
            <a-input-number
              v-model:value="stopForm.stopPrice"
              :min="0.0001"
              :precision="4"
              :step="0.01"
              placeholder="触发卖出的价格"
              style="width: 100%"
            />
            <div class="stop-presets">
              <a-button size="small" @click="fillStopFrom('suggested_stop_price')">
                建议 {{ stopModalRecord.suggested_stop_price == null ? '—' : formatMoney(stopModalRecord.suggested_stop_price).replace(/ [A-Z]{3}$/, '') }}
              </a-button>
              <a-button size="small" danger @click="fillStopFrom('mandatory_stop_price')">
                必须 {{ stopModalRecord.mandatory_stop_price == null ? '—' : formatMoney(stopModalRecord.mandatory_stop_price).replace(/ [A-Z]{3}$/, '') }}
              </a-button>
            </div>
          </label>
          <label class="calculator-field">
            <span>止损数量</span>
            <a-input-number
              v-model:value="stopForm.stopQty"
              :min="0.0001"
              :max="toNumber(stopModalRecord.quantity)"
              :precision="4"
              placeholder="ALL"
              style="width: 100%"
            />
            <div class="stop-presets">
              <a-button
                size="small"
                :type="stopForm.stopQty == null ? 'primary' : 'default'"
                @click="stopForm.stopQty = null"
              >
                ALL
              </a-button>
            </div>
          </label>
        </div>
        <a-alert
          v-if="holdingStopHit({ ...stopModalRecord, stop_price: stopForm.stopPrice }, 'stop_price')"
          type="error"
          show-icon
          message="现价已触及止损价。"
        />
        <a-button type="link" danger :disabled="!stopFormHasValue || stopModalSaving" @click="clearStopModal">
          清除止损
        </a-button>
      </div>
    </a-modal>

    <a-modal
      v-model:open="limitModalOpen"
      :title="`${tickerOf(limitModalRecord?.symbol)} 仓位`"
      width="440px"
      wrap-class-name="mobile-sheet-modal holdings-stop-modal"
      centered
      destroy-on-close
      :confirm-loading="limitModalSaving"
      ok-text="保存"
      @ok="saveLimitModal"
    >
      <div v-if="limitModalRecord" class="stop-modal">
        <div class="calculator-overview">
          <div>
            <span>当前数量</span>
            <strong>{{ formatQty(limitModalRecord.quantity) }}</strong>
          </div>
          <div>
            <span>已投入</span>
            <strong>{{ formatMoney(limitModalRecord.lot_cost_basis || limitModalRecord.cost_basis) }}</strong>
          </div>
          <div>
            <span>当前市值</span>
            <strong>{{ formatMoney(limitModalRecord.market_value) }}</strong>
          </div>
        </div>
        <p class="calculator-note">填仓位股数、最大买入金额，或两项都填。当前持仓超过任一项即视为超限。</p>
        <div class="stop-modal-form">
          <label class="calculator-field">
            <span>仓位（股）</span>
            <a-input-number
              v-model:value="limitForm.targetQty"
              :min="0.0001"
              :precision="4"
              :step="1"
              placeholder="计划持有的股数"
              style="width: 100%"
            />
          </label>
          <label class="calculator-field">
            <span>最大买入金额</span>
            <a-input-number
              v-model:value="limitForm.maxBuyAmount"
              :min="0.01"
              :precision="2"
              :step="100"
              placeholder="该标的最多投入"
              style="width: 100%"
            />
          </label>
        </div>
        <a-alert
          v-if="limitModalPreview.ok === false"
          type="error"
          show-icon
          :message="`当前仓位${limitModalPreview.label}`"
        />
        <a-button type="link" danger :disabled="!limitFormHasValue || limitModalSaving" @click="clearLimitModal">
          清除仓位设置
        </a-button>
      </div>
    </a-modal>

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

.price-day-change {
  margin-left: 6px;
  font-size: 12px;
  font-weight: 600;
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

.avg-price-chart {
  width: 100%;
  height: 380px;
}

.holding-row-actions {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
}

.holding-action-link {
  height: 22px;
  padding-inline: 6px;
  font-size: 12px;
  line-height: 20px;
  border-radius: 6px;
  background: #f0f3fa;
}

.holding-action-link:hover {
  background: #e8efff !important;
}

.holding-action-link.is-set {
  color: #315ea8;
  font-weight: 650;
}

.holding-action-link.stop-danger {
  color: #b52f27;
  background: #fff5f6;
  font-weight: 750;
}

.holding-stop-stack,
.holding-limit-stack {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  width: 100%;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: pointer;
  text-align: right;
  font: inherit;
  color: inherit;
}

.holding-limit-line,
.holding-limit-status {
  font-size: 11px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  line-height: 1.25;
  white-space: nowrap;
}

.holding-stop-tag {
  display: inline-block;
  max-width: 100%;
  padding: 0 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  line-height: 18px;
  white-space: nowrap;
}

.holding-stop-tag.is-suggest {
  color: #8a6400;
  background: #fff4cc;
}

.holding-stop-tag.is-must,
.holding-stop-tag.is-set {
  color: #b52f27;
  background: #ffe8ea;
}

.holding-stop-tag.is-target {
  color: #067a68;
  background: #e7f6f2;
}

.holding-limit-status {
  font-size: 12px;
  font-weight: 750;
}

.holding-limit-stack.pnl-up .holding-limit-status {
  color: #089981;
}

.holding-limit-stack.pnl-down .holding-limit-status {
  color: #b52f27;
}

.holding-limit-line {
  color: #787b86;
}

.positions-table :deep(.symbol-meta .name),
.cumulative-table :deep(.symbol-meta .name) {
  max-width: 72px;
}

.positions-table :deep(.ant-table-cell-fix-left),
.positions-table :deep(.ant-table-cell-fix-right),
.cumulative-table :deep(.ant-table-cell-fix-left),
.cumulative-table :deep(.ant-table-cell-fix-right),
.closed-table :deep(.ant-table-cell-fix-left),
.holdings-table :deep(.ant-table-cell-fix-left) {
  background: #fff;
}

.positions-table :deep(.ant-table-cell-fix-right-first),
.cumulative-table :deep(.ant-table-cell-fix-right-first) {
  box-shadow: -8px 0 12px -12px rgba(19, 23, 34, 0.5);
}

.cumulative-table :deep(.ant-table-row-expand-icon-cell),
.closed-table :deep(.ant-table-row-expand-icon-cell) {
  width: 36px;
  min-width: 36px;
  padding-inline: 0 !important;
  text-align: center;
}

.add-position-calculator {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.calculator-overview,
.calculator-result-grid {
  display: grid;
  gap: 10px;
}

.calculator-overview {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding: 14px;
  border-radius: 10px;
  background: linear-gradient(135deg, #f7f9fc 0%, #eef3ff 100%);
  border: 1px solid #e5eaf4;
}

.calculator-overview > div,
.calculator-result {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.calculator-overview span,
.calculator-result span,
.calculator-field > span {
  color: #787b86;
  font-size: 12px;
}

.calculator-overview strong {
  color: #131722;
  font-size: 15px;
  font-variant-numeric: tabular-nums;
}

.calculator-overview small,
.calculator-result small {
  font-size: 12px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.calculator-form-grid,
.stop-modal-form {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}

.stop-modal {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.stop-modal .calculator-overview {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.stop-modal-form {
  grid-template-columns: 1fr 1fr;
}

.stop-presets {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.stop-modal > .ant-btn-link {
  align-self: flex-start;
  padding-inline: 0;
}

.calculator-field {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.calculator-field small {
  color: #6d7482;
  font-size: 12px;
  line-height: 1.45;
}

.calculator-field small.feasible {
  color: #315ea8;
  font-weight: 650;
}

.calculator-result-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.calculator-result {
  min-width: 0;
  padding: 12px;
  border: 1px solid #e5e8ee;
  border-radius: 9px;
  background: #fff;
}

.calculator-result strong {
  color: #131722;
  font-size: 16px;
  font-variant-numeric: tabular-nums;
}

.calculator-note {
  margin: 0;
  color: #787b86;
  font-size: 12px;
  line-height: 1.6;
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

  .holdings-table :deep(.ant-table-cell-fix-left) {
    padding-inline: 4px !important;
  }

  .cumulative-table :deep(.ant-table-row-expand-icon-cell),
  .closed-table :deep(.ant-table-row-expand-icon-cell) {
    width: 24px !important;
    min-width: 24px !important;
    padding-inline: 0 !important;
  }

  .cumulative-table :deep(.ant-table-row-expand-icon),
  .closed-table :deep(.ant-table-row-expand-icon) {
    margin: 0;
  }

  .holdings-table :deep(.symbol-cell) {
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 3px;
    margin: 0;
    padding: 0;
  }

  .holdings-table :deep(.symbol-logo) {
    width: 24px;
    height: 24px;
  }

  .holdings-table :deep(.symbol-meta) {
    text-align: center;
  }

  .holdings-table :deep(.symbol-meta .code) {
    font-size: 11px;
    line-height: 1.1;
  }

  .holdings-table :deep(.symbol-meta .name) {
    display: none;
  }

  .trade-steps {
    max-height: calc(100dvh - 150px);
    padding-right: 2px;
  }

  .avg-price-chart {
    height: 280px;
  }
}

@media (max-width: 560px) {
  .add-position-calculator {
    gap: 12px;
  }

  .calculator-overview,
  .calculator-result-grid {
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }

  .calculator-overview {
    padding: 10px;
  }

  .calculator-overview strong,
  .calculator-result strong {
    font-size: 14px;
  }

  .calculator-result {
    padding: 10px;
  }

  .calculator-form-grid,
  .stop-modal-form {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .stop-modal .calculator-overview {
    grid-template-columns: 1fr 1fr;
  }

  .calculator-field small {
    word-break: break-word;
  }

  .add-position-calculator :deep(.ant-input-number),
  .stop-modal :deep(.ant-input-number) {
    min-height: 40px;
  }

  .add-position-calculator :deep(.ant-input-number-input),
  .stop-modal :deep(.ant-input-number-input) {
    height: 40px;
    font-size: 16px;
  }

  .add-position-calculator :deep(.ant-input-number-group-addon) {
    font-size: 14px;
  }
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.advice-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.advice-body .trend-brief {
  margin-bottom: 0;
}

.advice-body .trend-brief-grid,
.advice-body .trend-brief-grid article:last-child {
  grid-template-columns: 1fr;
  grid-column: auto;
}

.advice-headline {
  margin: 0;
  font-size: 14px;
  line-height: 1.6;
}

.advice-point,
.advice-card p {
  margin: 0;
  color: var(--text);
  font-size: 13px;
  line-height: 1.65;
}

.price-page {
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  padding: 8px 0 32px;
  color: #243047;
}

.price-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  margin-bottom: 26px;
}

.price-eyebrow {
  margin: 0 0 8px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: #7b8598;
}

.price-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  font-size: 27px;
  font-weight: 700;
  letter-spacing: -0.04em;
  line-height: 1.3;
}

.price-title-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #14977d;
}

.price-description {
  margin: 8px 0 0;
  font-size: 13px;
  line-height: 1.6;
  color: #7b8598;
}

.price-header-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px;
}

.price-page :deep(.price-action.ant-btn) {
  height: 36px;
  border-radius: 9px;
  border-color: #dce3f4;
  color: #4662bf;
  background: #f9faff;
  font-size: 12px;
  box-shadow: none;
}

.price-hero {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(390px, 0.92fr);
  gap: 34px;
  align-items: center;
  overflow: hidden;
  margin-bottom: 16px;
  padding: 34px;
  border: 1px solid #dde9e5;
  border-radius: 20px;
  background: linear-gradient(122deg, #f4faf7 0%, #f8fbfe 50%, #eef3ff 100%);
}

.price-hero::after {
  content: '';
  position: absolute;
  right: -76px;
  top: -108px;
  width: 250px;
  height: 250px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(80, 109, 185, 0.1), rgba(80, 109, 185, 0));
  pointer-events: none;
}

.price-hero-copy,
.price-metrics {
  position: relative;
  z-index: 1;
}

.price-kicker {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 15px;
  font-size: 12px;
  font-weight: 600;
  color: #60766f;
}

.price-kicker-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #14977d;
  box-shadow: 0 0 0 4px rgba(20, 151, 125, 0.1);
}

.price-hero-copy h2 {
  margin: 0;
  font-size: clamp(30px, 3.6vw, 43px);
  font-weight: 680;
  line-height: 1.24;
  letter-spacing: -0.05em;
  color: #21836f;
}

.price-hero-note {
  max-width: 600px;
  margin: 17px 0 0;
  font-size: 13px;
  line-height: 1.85;
  color: #66778b;
}

.price-hero-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  margin-top: 21px;
}

.price-hero-meta span {
  padding: 5px 9px;
  border: 1px solid rgba(210, 222, 230, 0.9);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.58);
  font-size: 10px;
  color: #687b89;
}

.price-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.price-metrics article {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  min-width: 0;
  min-height: 118px;
  padding: 17px;
  border: 1px solid rgba(219, 227, 239, 0.95);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.84);
  box-shadow: 0 5px 18px rgba(46, 70, 105, 0.035);
}

.price-metric-icon {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 32px;
  height: 32px;
  border-radius: 9px;
  font-size: 15px;
}

.price-metric-icon.blue { color: #4b6de0; background: #eef2ff; }
.price-metric-icon.teal { color: #14977d; background: #eaf7f3; }
.price-metric-icon.amber { color: #b78631; background: #fcf5e8; }
.price-metric-icon.violet { color: #8a68c9; background: #f3effb; }

.price-metrics p {
  margin: 1px 0 7px;
  font-size: 10px;
  color: #7c8798;
}

.price-metrics strong {
  display: block;
  font-size: 21px;
  line-height: 1.22;
  letter-spacing: -0.035em;
  font-variant-numeric: tabular-nums;
  color: #31445b;
}

.price-metrics strong.pnl-up,
.price-tape strong.pnl-up,
.price-tape span.pnl-up {
  color: #089981;
}

.price-metrics strong.pnl-down,
.price-tape strong.pnl-down,
.price-tape span.pnl-down {
  color: #f23645;
}

.price-metrics article > div > span {
  display: block;
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.5;
  color: #929baa;
}

.price-tape {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 8px;
  padding: 18px;
  border: 1px solid #dfe8e4;
  border-radius: 16px;
  background: linear-gradient(180deg, #fbfdfc, #f7fafb);
}

.price-tape article {
  min-width: 0;
  padding: 14px 15px;
  border: 1px solid #e5ece9;
  border-radius: 11px;
  background: rgba(255, 255, 255, 0.72);
}

.price-tape p {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 650;
  color: #425969;
}

.price-tape strong {
  display: block;
  font-size: 20px;
  line-height: 1.2;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}

.price-tape article > span {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: #8994a1;
}

.price-tape .sentiment-chart {
  grid-column: 1 / -1;
  height: 228px;
  margin-top: 4px;
}

.price-section-head {
  margin-top: 28px;
}

.price-section-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 0 0 8px;
  font-size: 19px;
  font-weight: 650;
  letter-spacing: -0.025em;
}

.price-section-number {
  padding: 5px 6px;
  border-radius: 5px;
  background: #eaf5f1;
  color: #3c8a77;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.price-section-badge {
  padding: 3px 8px;
  border-radius: 5px;
  background: #f3f5f9;
  color: #8992a4;
  font-size: 11px;
  font-weight: 400;
}

.price-section-sub {
  margin: 0 0 14px;
  font-size: 12px;
  line-height: 1.7;
  color: #7b8598;
}

.price-toolbar {
  margin-bottom: 12px;
}

.price-page .price-table :deep(.ant-table) {
  border: 1px solid #e7ebf2;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 16px rgba(38, 58, 95, 0.025);
}

.price-page .price-table :deep(.ant-table-thead > tr > th) {
  background: #f8f9fc;
  color: #7b8598;
  font-size: 12px;
  font-weight: 600;
}

.sentiment-note {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.sentiment-chart,
.trend-chart {
  width: 100%;
}

.sentiment-chart {
  height: 200px;
}

@media (max-width: 1100px) {
  .price-hero {
    grid-template-columns: 1fr;
    gap: 26px;
    padding: 28px;
  }
}

@media (max-width: 800px) {
  .price-header {
    align-items: flex-start;
  }

  .price-hero {
    padding: 24px;
  }

  .price-tape {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
    padding: 12px;
  }

  .price-tape article {
    padding: 10px 8px;
  }

  .price-tape p,
  .price-tape article > span {
    font-size: 11px;
  }

  .price-tape strong {
    font-size: 15px;
  }
}

@media (max-width: 560px) {
  .price-header {
    flex-direction: column;
    gap: 16px;
    margin-bottom: 20px;
  }

  .price-title {
    font-size: 24px;
  }

  .price-header-actions {
    width: 100%;
  }

  .price-hero {
    padding: 22px 18px;
    border-radius: 16px;
  }

  .price-hero-copy h2 {
    font-size: 28px;
  }

  .price-metrics article {
    min-height: 0;
    padding: 13px;
  }

  .price-metrics strong {
    font-size: 17px;
  }
}

.col-head {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}

.col-hint {
  color: var(--muted);
  font-size: 12px;
  cursor: help;
}

.trend-toolbar {
  display: flex;
  margin-bottom: 8px;
}

.trend-toolbar :deep(.ant-select) {
  width: 280px;
}

.trend-marks {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.trend-mark {
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 650;
  line-height: 20px;
}

.trend-mark.is-up {
  color: #067a68;
  background: #e7f6f2;
}

.trend-mark.is-down {
  color: #b52f27;
  background: #ffe8ea;
}

.trend-mark.is-warn {
  color: #8a6400;
  background: #fff4cc;
}

.trend-mark.is-info {
  color: #315ea8;
  background: #e8efff;
}

.trend-quote {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0 0 6px;
  padding: 10px 12px 8px;
  border: 1px solid #e7ebf2;
  border-radius: 12px;
  background: #fff;
  font-variant-numeric: tabular-nums;
}

.trend-quote-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.trend-quote-date {
  color: #7b8598;
  font-size: 12px;
  font-weight: 650;
}

.trend-quote-last {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.trend-quote-last b:first-child {
  font-size: 20px;
  line-height: 1.1;
  letter-spacing: -0.03em;
}

.trend-quote b {
  font-weight: 650;
}

.trend-quote-change {
  font-size: 13px;
}

.trend-quote-grid {
  display: flex;
  gap: 6px;
}

.trend-quote-grid span {
  display: flex;
  flex: 1 1 0;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  padding: 6px 8px;
  border-radius: 8px;
  background: #f8f9fc;
}

.trend-quote-grid em {
  color: #7b8598;
  font-size: 11px;
  font-style: normal;
  font-weight: 600;
  line-height: 1.2;
}

.trend-quote-grid b {
  color: #243047;
  font-size: 13px;
  line-height: 1.2;
}

.trend-quote .is-ma b {
  color: #e6a700;
}

.trend-quote .is-vwap b {
  color: #089981;
}

.trend-chart {
  height: 460px;
  margin-bottom: 8px;
}

.trend-table {
  max-width: 100%;
}

.trend-table :deep(.ant-table) {
  border: 1px solid #e7ebf2;
  border-radius: 14px;
  overflow: hidden;
}

.trend-table :deep(.ant-table-thead > tr > th) {
  background: #f8f9fc;
  color: #7b8598;
  font-size: 12px;
  font-weight: 600;
}

.trend-brief {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 8px;
}

.trend-brief-lead {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  line-height: 1.55;
  letter-spacing: -0.02em;
  color: #243047;
}

.trend-levels {
  display: flex;
  gap: 8px;
  width: 100%;
  margin-bottom: 8px;
}

.trend-levels div,
.trend-levels button {
  display: flex;
  flex: 1 1 0;
  align-items: baseline;
  justify-content: center;
  gap: 8px;
  min-width: 0;
  padding: 10px 12px;
  border-radius: 10px;
  font: inherit;
}

.trend-levels button {
  cursor: pointer;
}

.trend-levels button:hover {
  filter: brightness(0.97);
}

.trend-levels span {
  font-size: 12px;
  font-weight: 650;
}

.trend-levels strong {
  font-size: 18px;
  line-height: 1.2;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}

.trend-levels .is-stop {
  color: #f23645;
  background: #fff5f6;
  border: 1px solid #ffd6da;
}

.trend-levels .is-target {
  color: #089981;
  background: #f3fbf8;
  border: 1px solid #cde8df;
}

.trend-brief-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.trend-brief-grid article {
  padding: 12px 14px;
  border: 1px solid #e5ece9;
  border-radius: 12px;
  background: linear-gradient(180deg, #fbfdfc, #f7fafb);
}

.trend-brief-grid h3 {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 650;
  color: #3c8a77;
}

.trend-brief-grid p {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
}

.trend-brief-advice {
  margin-top: 4px;
  color: var(--muted);
}

.trend-brief-grid article:last-child {
  grid-column: 1 / -1;
}

@media (max-width: 720px) {
  .trend-toolbar {
    flex-direction: column;
  }

  .trend-toolbar :deep(.ant-select) {
    width: 100% !important;
  }

  .trend-brief-grid {
    grid-template-columns: 1fr;
  }

  .trend-quote-grid {
    flex-wrap: wrap;
  }

  .trend-quote-grid span {
    flex: 1 1 22%;
  }

  .trend-chart {
    height: 320px;
  }
}

.advice-note {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}

.advice-card {
  border-top: 1px solid var(--border);
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.advice-card-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.advice-card-head strong {
  font-size: 15px;
}

.advice-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 8px;
}

.advice-metric {
  background: var(--chip);
  border-radius: 6px;
  padding: 6px 8px;
}

.advice-metric span,
.advice-metric small {
  display: block;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.3;
}

.advice-metric strong {
  display: block;
  margin-top: 2px;
  font-size: 13px;
  font-weight: 600;
}

.advice-name {
  color: var(--muted);
  font-size: 12px;
}

.advice-stage {
  font-size: 12px;
  font-weight: 650;
  line-height: 20px;
  padding: 3px 8px;
  border-radius: 6px;
  background: #f4f6fa;
  color: #5c6b82;
}

.advice-stage.is-warning,
.advice-stage.is-danger {
  color: #b52f27;
  background: #ffe8ea;
}

.advice-stage.is-success {
  color: #067a68;
  background: #e7f6f2;
}

.advice-stage.is-info {
  color: #315ea8;
  background: #e8efff;
}

.price-table :deep(th.ant-table-cell-fix-left),
.price-table :deep(td.ant-table-cell-fix-left) {
  width: 120px;
  max-width: 120px;
}

.price-table :deep(.symbol-meta .name) {
  max-width: 68px;
}

.price-table :deep(.ant-table-tbody > tr) {
  cursor: pointer;
}

.toolbar-actions .advice-note {
  max-width: 420px;
}
</style>
