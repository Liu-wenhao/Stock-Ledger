<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import dayjs from 'dayjs'
import { message, Modal } from 'ant-design-vue'
import { DashboardOutlined, DownloadOutlined, FallOutlined, LineChartOutlined, QuestionCircleOutlined, RiseOutlined } from '@ant-design/icons-vue'
import { fetchHoldings, fetchHoldingsAdvice, fetchIndicatorHistory, fetchBreadthHistory, fetchCnDesk, fetchCnFlow, fetchMarketSentiment, fetchTrades, updateHoldingLimit, updateHoldingStop } from '@/api/portfolio'
import SymbolCell from '@/components/SymbolCell.vue'
import { formatMoney, formatPct, formatQty, pnlClass, tickerOf, toNumber } from '@/utils/format'
import { isMarketOpen } from '@/utils/marketHours'
import { usePortfolioStore } from '@/stores/portfolio'
import { downloadTableImage } from '@/utils/tableImage'
import { useTableScrollY } from '@/composables/useTableScroll'
import { useTheme } from '@/composables/useTheme'

const router = useRouter()
const store = usePortfolioStore()
const { isDark } = useTheme()

const emit = defineEmits(['changed'])
const cnDesk = ref(null)
const cnFlow = ref(null)
const upBuckets = computed(() =>
  (cnDesk.value?.activity?.buckets || []).filter((bucket) => String(bucket.label).startsWith('上涨')),
)
const downBuckets = computed(() =>
  (cnDesk.value?.activity?.buckets || []).filter((bucket) => String(bucket.label).startsWith('下跌')),
)
async function loadCnFlow() {
  if (store.market !== 'cn') {
    cnFlow.value = null
    return
  }
  try {
    cnFlow.value = await fetchCnFlow()
  } catch {
    cnFlow.value = null
  }
}

async function loadCnDesk() {
  if (store.market !== 'cn') {
    cnDesk.value = null
    return
  }
  try {
    cnDesk.value = await fetchCnDesk()
  } catch {
    cnDesk.value = null
  }
}

function flowTone(label) {
  return { 超大单: 'amber', 大单: 'blue', 中单: 'teal', 小单: 'violet' }[label] || 'amber'
}
function flowYi(value) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}亿`
}
const flowTiers = computed(() => {
  const flow = cnDesk.value?.flow
  if (!flow) return []
  const ratio = (value) => (value == null || Number.isNaN(Number(value)) ? '' : `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(2)}%`)
  return [
    ['主力', flow.main, flow.main_ratio],
    ['超大单', flow.super_order, flow.super_ratio],
    ['大单', flow.large, flow.large_ratio],
    ['中单', flow.mid, flow.mid_ratio],
    ['小单', flow.small, flow.small_ratio],
  ].map(([label, amount, share]) => ({
    label,
    amount,
    share,
    text: `${label} ${flowYi(amount)}${ratio(share)}`,
  }))
})
const subTab = ref('positions')
const keyword = ref('')
const loading = ref(false)
const data = ref({ items: [], total_market_value: 0, cash: 0 })
const tableScrollY = useTableScrollY(420)
const viewportWidth = ref(window.innerWidth)
const tableScroll = computed(() => ({
  x: viewportWidth.value > 560
    ? 1720
    : subTab.value === 'cumulative'
      ? 603
      : subTab.value === 'closed'
        ? 687
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
const tradeModalPrice = ref(null)
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
      q: keyword.value || undefined,
      sort_by: sortState.field || undefined,
      sort_order: sortState.order || undefined,
    })
    if (!silent && subTab.value === 'positions') loadIndicators()
  } finally {
    if (!silent) loading.value = false
  }
}

async function autoRefreshQuotes() {
  if (loading.value || !isMarketOpen(store.market)) return
  await load({ silent: true })
}

function tradeInCycle(t, startMs, endMs) {
  const ts = new Date(t.closing_time).getTime()
  if (startMs != null && ts < startMs) return false
  if (endMs != null && ts > endMs) return false
  return true
}

async function openTradeHistory(record, cycle = null) {
  tradeModalSymbol.value = record.symbol
  tradeModalPrice.value = record.last_price
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
  const vs = tradeVsNow(t)
  const ratio =
    vs == null
      ? ''
      : ` <span style="color:var(--text)!important">(</span><span class="${pnlClass(vs)}">${formatPct(vs * 100)}</span><span style="color:var(--text)!important">)</span>`
  const base = `${SIDE_LABELS[t.side] || t.side}  ${formatMoney(t.fill_price, 'USD', 4).replace(/ [A-Z]{3}$/, '')} × ${formatQty(t.qty)}${ratio}`
  if (clearTradeIds.value.has(t.id)) return `${base}  · 清仓`
  return base
}

function goEditTrade(t) {
  if (!t?.id) return
  tradeModalOpen.value = false
  router.push({ name: 'trades', query: { editId: String(t.id) } })
}

function tradeVsNow(t) {
  // Buy is ahead when the latest price is above the fill. Sell is ahead when the fill is above the latest price.
  const now = toNumber(tradeModalPrice.value)
  const fill = toNumber(t.fill_price)
  if (!(now > 0) || !(fill > 0)) return null
  if (t.side === 'Buy') return (now - fill) / fill
  if (t.side === 'Sell') return (fill - now) / fill
  return null
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
  { title: '信号', key: 'signals', width: 132, align: 'center' },
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
  signals: 96,
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

const adviceLoading = ref(false)
const indicatorBook = ref(null)
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

const INDICATOR_POLL_MS = 60_000
let lastIndicatorPoll = 0

async function loadIndicators({ silent = false } = {}) {
  if (subTab.value !== 'positions') return
  if (!silent) adviceLoading.value = true
  try {
    indicatorBook.value = await fetchHoldingsAdvice()
  } catch {
    if (!silent) indicatorBook.value = null
  } finally {
    if (!silent) adviceLoading.value = false
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

function signalListOf(record) {
  const metrics = metricsOf(record.symbol)
  if (!metrics) return null
  return metrics.signals || []
}

function signalTitle(record) {
  const list = signalListOf(record) || []
  return list
    .map((item) => `${item.name}：${item.detail || '条件都满足'}${item.exit_now ? ` · ${item.exit_now}` : ''}`)
    .join('\n')
}

function imageSignals(record) {
  const list = signalListOf(record)
  if (!list) return '—'
  if (!list.length) return { text: '未触发', tone: '' }
  return { text: list.map((item) => item.name).join('、'), tone: 'pnl-up' }
}

const sentiment = ref(null)
const sentimentState = ref('idle')
const sentimentChartRef = ref(null)
let sentimentChart

const trendOpen = ref(false)
const trendLoading = ref(false)
const trendSymbol = ref('')
const trendScope = ref([])
async function loadTrendScope() {
  try {
    const holdings = await fetchHoldings({ include_closed: true })
    trendScope.value = holdings.items || []
  } catch {
    trendScope.value = []
  }
}
const trendSelectOptions = computed(() => {
  const items = trendScope.value
  const labelOf = (item) => item.name || tickerOf(item.symbol)
  const held = items.filter((item) => !item.closed && toNumber(item.quantity) > 0)
  const closed = items.filter((item) => item.closed && toNumber(item.trade_count) > 0)
  const option = (item) => ({ value: item.symbol, label: labelOf(item) })
  const groups = []
  if (held.length) groups.push({ label: '持仓', options: held.map(option) })
  if (closed.length) groups.push({ label: '清仓', options: closed.map(option) })
  return groups
})
const trendEligibleSymbols = computed(() =>
  trendSelectOptions.value.flatMap((group) => group.options.map((option) => option.value)),
)
const trendBook = ref(null)
const trendChartRef = ref(null)
let trendChart
let trendLoadToken = 0
let trendPaging = false
let trendZoomLive = false
let trendZoomStamp = 0
let trendZoomGuard = null
const TREND_PAGE = 40

const trendBaseColumns = [
  { title: '日期', dataIndex: 'date', key: 'date', width: 108, fixed: 'left' },
  { title: '信号', key: 'signals', width: 132, align: 'center' },
  { title: '收盘', dataIndex: 'close', key: 'close', align: 'right', width: 88 },
  { title: '日涨跌', key: 'day_change', align: 'right', width: 96 },
  { title: '日涨跌 %', key: 'day_change_percent', align: 'right', width: 84 },
  { title: '开盘', key: 'open', align: 'right', width: 80 },
  { title: '最高', key: 'high', align: 'right', width: 80 },
  { title: '最低', key: 'low', align: 'right', width: 80 },
  { title: '昨收', key: 'prev_close', align: 'right', width: 80 },
  { title: '成交量', key: 'volume', align: 'right', width: 100 },
  { title: '成交额', key: 'turnover', align: 'right', width: 100 },
  { title: 'Amihud', key: 'illiq', align: 'right', width: 96 },
  { title: '锚定日', key: 'anchor_date', align: 'right', width: 108 },
  { title: 'AVWAP', key: 'anchor_vwap', align: 'right', width: 100 },
  { title: '距锚', key: 'vs_anchor', align: 'right', width: 84 },
  { title: 'MA5', key: 'ma5', align: 'right', width: 88 },
  { title: 'MA10', key: 'ma10', align: 'right', width: 88 },
  { title: 'MA20', key: 'ma20', align: 'right', width: 96 },
  { title: '排列', key: 'ma_order', align: 'right', width: 72 },
  { title: '带宽', key: 'bandwidth', align: 'right', width: 84 },
  { title: '波动', key: 'vol', align: 'right', width: 84 },
  { title: '动量', key: 'mom', align: 'right', width: 80 },
  { title: '仓位上限', key: 'vol_cap', align: 'right', width: 100 },
  { title: '平价', key: 'rp_weight', align: 'right', width: 80 },
  { title: 'PE', key: 'pe', align: 'right', width: 72 },
  { title: 'ERP', key: 'erp', align: 'right', width: 80 },
  { title: '距均线', key: 'vs_ma20', align: 'right', width: 92 },
  { title: 'RSI', key: 'rsi', align: 'right', width: 72 },
  { title: '止损K线', key: 'stop_candle', align: 'right', width: 96 },
]
const trendColumns = computed(() => store.market === 'cn'
  ? [
      ...trendBaseColumns.slice(0, 4),
      { title: '主力', key: 'main_flow', align: 'right', width: 96 },
      { title: '买盘', key: 'buy_volume', align: 'right', width: 100 },
      { title: '卖盘', key: 'sell_volume', align: 'right', width: 100 },
      ...trendBaseColumns.slice(4),
    ]
  : trendBaseColumns,
)
const trendTableScroll = computed(() => ({
  x: trendColumns.value.reduce((width, column) => width + column.width, 0),
  y: 280,
}))
const trendUnavailableHints = {
  main_flow: '日线库未保存历史主力净流入，不用当前资金流替代。',
  buy_volume: '日线库未保存历史买盘，不用当前买盘替代。',
  sell_volume: '日线库未保存历史卖盘，不用当前卖盘替代。',
  rp_weight: '平价依赖当天组合和各标的波动率；未保存历史组合快照，不用当前权重替代。',
  pe: '未保存历史 PE，不用当前市盈率替代。',
  erp: '缺少历史 PE 和对应日期的国债收益率，不用当前 ERP 替代。',
}

const trendCursor = ref(null)
const trendRows = computed(() => [...(trendBook.value?.points || [])].reverse())
const trendLatest = computed(() => {
  const points = trendBook.value?.points || []
  return points.length ? points[points.length - 1] : null
})
const indicatorHints = {
  volume: {
    text: '已收盘交易日的成交股数。',
    up: '价涨同时成交量走高更好，说明有人接；缩量上涨偏弱。',
    range: '放量但价格几乎不动，多是换手；缩量横盘是常态。',
    down: '放量下跌更差；缩量下跌说明抛压在减轻。',
  },
  turnover: {
    text: '收盘价乘以成交量。缩量上涨是上涨日成交额低于近 20 日的 80%；放量滞涨是涨跌不到 0.5% 且成交额高于近 20 日的 1.8 倍。',
    up: '上涨日成交额走高更好；低于近 20 日 80% 的缩量上涨偏弱。',
    range: '涨跌不到 0.5% 却放到 1.8 倍，是滞涨，不是方向。',
    down: '下跌日成交额走高更差；走低说明杀跌在减弱。',
  },
  illiq: {
    text: '当日涨跌幅绝对值（百分点）除以成交额（亿美元）。柱高表示买卖有多费劲。颜色另标成交额形态：绿色是缩量上涨，当天上涨超过 0.5% 且成交额低于近 20 日的 80%；金色是放量滞涨，当天涨跌不到 0.5% 且成交额高于近 20 日的 1.8 倍；红色是缩量下跌，当天下跌超过 0.5% 且成交额低于近 20 日的 80%；其余是灰色。',
    up: '数值走低更好，上涨不需要很大的价格冲击。',
    range: '走低说明好成交；走高只是变难交易，不代表方向。',
    down: '数值走高更差，同样的卖出把价格打得更低。',
  },
  anchor_date: {
    text: '近 20 个交易日最低价所在的日期。防守线从这一天起向后累积。',
    up: '锚定日停在较早的低点、不再后移更好，说明低点没有继续下移。',
    range: '锚定日在区间里来回换，不表示方向。',
    down: '锚定日不断改到最近的新低，更差。',
  },
  anchor_vwap: {
    text: '从锚定日起，用 (高+低+收)/3 按成交量加权。图上是绿色虚线。',
    up: '这条线往上更好，回踩不跌破说明持仓成本还在抬高。',
    range: '线走平是常态，价格在线上下穿不算趋势。',
    down: '线往下是防守成本在降；走平或重新上拐才算转好。',
  },
  vs_anchor: {
    text: '价格相对 AVWAP 的偏离。大于等于 0 为机构持仓安全，小于 0 为机构防守线失守。',
    up: '维持在 0 上方更好；离得太远只说明涨多了，不因此加仓。',
    range: '在 0 附近来回，不单独当买卖。',
    down: '负得更多更差。往 0 靠是修复，收复前仍按失守处理。',
  },
  ma5: {
    text: '近 5 个交易日收盘价的平均数，三条均线里反应最快。',
    up: '均线往上、价格在它上方更好。',
    range: '走平是常态，上下穿不算趋势。',
    down: '均线往下是短线还在跌；走平或转上只是缓和，要看更长的均线。',
  },
  ma10: {
    text: '近 10 个交易日收盘价的平均数。',
    up: '往上、并且位于 MA20 之上更好。',
    range: '走平，和 MA5、MA20 缠在一起就是交错。',
    down: '往下、并且压在 MA20 之下，跌势还在；单独转上还不能算结束。',
  },
  ma20: {
    text: '近 20 个交易日收盘价的平均数，也是布林带中轨。',
    up: '往上、收盘站在它上方更好。',
    range: '走平，价格在它附近来回。',
    down: '往下、收盘在它下方是跌势。重新站上才离开左侧，站上之前不加仓。',
  },
  ma_order: {
    text: 'MA5 高于 MA10、MA10 高于 MA20 为多头排列；反过来为空头排列；其余是交错。排列只描述三条均线，不代替价格和防守线。',
    up: '多头排列更好。价格仍在 MA20 下方时，排列多头也不当成追涨。',
    range: '交错是常态。',
    down: '空头排列是跌势还在。从空头变成交错只是缓和，不是反转。',
  },
  vs_ma20: {
    text: '收盘价相对 MA20 的偏离。',
    up: '正偏离更好。偏离过大只说明离均线远，不因此加仓。',
    range: '在 0 附近来回。',
    down: '负偏离扩大更差。负值收窄是修复，回到 0 上方才离开均线下方。',
  },
  bandwidth: {
    text: '(上轨 − 下轨) / 中轨。上下轨是 MA20 加减 2 倍样本标准差。创近 120 个交易日新低时标变盘蓄势。带宽本身不分涨跌。',
    up: '从极窄再走阔、同时价格在涨，趋势在展开，更好。',
    range: '走窄是收敛，方向还没选。',
    down: '走阔同时价格在跌，跌势在加速，更差。',
  },
  vol: {
    text: '近 20 日收益率的样本标准差乘以 √252，得到实际年化波动。',
    up: '波动走低、价格仍往上，涨得更稳。突然走高可能是高潮，不是加仓理由。',
    range: '走低是安静的区间；走高往往是要选择方向。',
    down: '波动走高更差。走低只说明跌势变慢，不是已经见底。',
  },
  rsi: {
    text: '14 日 RSI，用 AlphaEvo 的 Wilder 平滑。30 以下是超卖，70 以上是超买。',
    up: '从 30 以下往上拐，超卖在修复。',
    range: '停在 30 到 70 之间，没有极端读数。',
    down: '高于 70 是超买；继续掉到 30 以下仍是杀跌。',
  },
  stop_candle: {
    text: 'AlphaEvo 的止损 K 线：下影线至少是实体的 2 倍，记为锤子线；或前一天收阴、当天收阳并包住前一天实体，记为看涨吞没。',
    up: '出现锤子线或看涨吞没，当天卖压有停住的样子。',
    range: '没有这两种形态。',
    down: '没有长下影，也没有吞没前一根阴线。',
  },
  mom: {
    text: '近 20 日累计涨跌幅除以年化波动。大于 0 为顺势，图底淡绿；小于 0 为左侧博弈，图底淡黄。',
    up: '大于 0 并且继续走高更好。',
    range: '靠近 0。',
    down: '小于 0 是跌势还在，更负更差。往 0 靠是缓和，转正之前仍按左侧处理。',
  },
  vol_cap: {
    text: '目标波动 20% 除以年化波动，作为这只股票的仓位上限。数值高只表示波动小、允许的仓位更大，不是涨跌信号。',
    up: '上限变高可以拿得更多，仍不超过这一列。',
    range: '按波动定大小，不按方向加仓。',
    down: '上限变高也不表示该买。跌势里先看价格和防守线。',
  },
  rp_weight: {
    text: '多头之间按波动的倒数分配。对冲腿不参与，这里留空。数值高表示这只波动更小、平价权重大。',
    up: '权重走高说明它相对更稳，可以在多头里占更大比例。',
    range: '权重只比较谁更稳，不表示即将突破。',
    down: '权重高也不把正在下跌的名字当成该加的仓。',
  },
  pe: {
    text: '行情里的滚动市盈率，并且要和价格除以每股收益对得上才采用。只作估值对照，不单独决定买卖。负的市盈率表示仍在亏损。',
    up: '走低通常是盈利跟上价格，更好。走高多半是价格涨出来的，不便宜，也不因此卖出。',
    range: '高低只说明贵贱，不选方向。',
    down: '走低常常是价格跌出来的，便宜不等于能买。',
  },
  erp: {
    text: '1/PE 减去美国 10 年期国债收益率。PE 缺失或不是正数时留空。越高表示盈利收益率比国债越高。',
    up: '走高是估值更有吸引力，更好，不单独作为买入。',
    range: '走高同样只是更便宜，方向还看价格。',
    down: '走高往往是价格下跌造成的，便宜不等于跌势结束。',
  },
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

function bookName(book) {
  return String(book?.universe || '').replace(/\s+\d+\s*家$/, '')
}

const INDEX_BOOK = {
  '标普 500': 'nyse',
  纳斯达克: 'nasdaq',
  沪深300: 'cn_all',
  科创50: 'cn_star',
  创业板指: 'cn_cyb',
}

function indexCaption(name) {
  return {
    '标普 500': '对照纽交所宽度',
    沪深300: '对照全市场 / 沪深300宽度',
    科创50: '对照科创板宽度',
    创业板指: '对照创业板宽度',
  }[name] || ''
}

function breadthWidth(breadth) {
  if (!breadth?.count) return null
  return ((Number(breadth.advancers) - Number(breadth.decliners)) / Number(breadth.count)) * 100
}

const armsOpen = ref(false)
const armsLoading = ref(false)
const armsHistory = ref(null)
const armsFocusDate = ref(null)
const breadthBook = ref('')
const sentimentBooks = computed(() => sentiment.value?.books || [])
const selectedBook = computed(
  () => sentimentBooks.value.find((item) => item.key === breadthBook.value) || sentiment.value?.breadth || null,
)
watch(sentimentBooks, (books) => {
  if (!books.some((item) => item.key === breadthBook.value)) breadthBook.value = books[0]?.key || ''
})
const armsChartRef = ref(null)
let armsChart
const ARMS_INDEX = { us: '纳斯达克 100', cn: '沪深300', hk: '恒生指数' }
const ARMS_PAIR_INDEX = {
  us_all: '纳斯达克 100',
  nyse: '标普 500',
  nasdaq: '纳斯达克 100',
  cn_all: '上证指数',
  cn_hs300: '沪深300',
  cn_star: '科创50',
  cn_cyb: '创业板指',
}
/** Modal scope for cn_all: full market by default, optional CSI 300. */
const armsScope = ref('full')
const canToggleArmsScope = computed(
  () => selectedBook.value?.key === 'cn_all' && Boolean(selectedBook.value?.narrow_key || selectedBook.value?.narrow),
)
const ARMS_UP = '#10b981'
const ARMS_DOWN = '#ef4444'

function disposeArmsChart() {
  if (!armsChart) return
  armsChart.off('updateAxisPointer')
  armsChart.off('globalout')
  armsChart.dispose()
  armsChart = null
}

function armsPosition(upRatio) {
  if (upRatio == null || !Number.isFinite(upRatio)) return null
  if (upRatio >= 80) return { badge: '极度亢奋 (超买预警)', tone: 'hot' }
  if (upRatio >= 50) return { badge: '健康多头趋势', tone: 'up' }
  if (upRatio >= 20) return { badge: '弱势结构分化', tone: 'down' }
  return { badge: '冰点超卖区 (左侧机会)', tone: 'ice' }
}

function armsTrinBadge(arms) {
  if (arms == null || !Number.isFinite(arms)) return { text: '没有读数', tone: 'flat' }
  if (arms < 0.45) return { text: '买盘过热 (短期超买)', tone: 'hot' }
  if (arms < 0.9) return { text: '多头资金强劲流入', tone: 'up' }
  if (arms <= 1.1) return { text: '多空资金均衡', tone: 'flat' }
  if (arms < 2) return { text: '空头抛压主导', tone: 'down' }
  return { text: '恐慌宣泄 (极度超卖底)', tone: 'ice' }
}

function armsPanelVerdict(adRatio, arms, ma20) {
  const ad = adRatio ?? 0
  const trin = arms ?? 0
  const width = ma20 == null || !Number.isFinite(Number(ma20)) ? null : Number(ma20)
  const widthText = width == null ? '还没有算出' : `${width.toFixed(1)}%`
  if (width != null && ad >= 1.5 && trin > 0 && trin < 0.85 && width >= 50) {
    return {
      icon: '🚀',
      tone: 'up',
      title: '真突破共振（高胜率顺势做多信号）',
      desc: `涨跌比达 ${ad.toFixed(2)} 且单日 TRIN 低至 ${trin.toFixed(2)}，高于 MA20 成分股占 ${widthText}。广度与真金白银买盘高度同步，并非少数权重虚拉，适合顺势持仓。`,
    }
  }
  if (ad >= 1.1 && trin > 1.15) {
    return {
      icon: '⚠️',
      tone: 'hot',
      title: '量价背离 / 虚胖式反弹（警惕暗中出货）',
      desc: `虽然表面上涨家数多于下跌家数（A/D=${ad.toFixed(2)}），但单日 TRIN 高达 ${trin.toFixed(2)} (>1.0)，且 MA20 宽度 ${widthText}，说明上涨股普遍缩量，而大资金正集中在下跌股中放量派发。`,
    }
  }
  if (width != null && trin >= 2 && width <= 25) {
    return {
      icon: '💎',
      tone: 'ice',
      title: '恐慌抛售极值（左侧反转 / 黄金坑信号）',
      desc: `高于 MA20 的比例跌至 ${widthText}，同时单日 TRIN 飙升至 ${trin.toFixed(2)} 极值，代表空头恐慌盘无差别涌出。此类极值后往往伴随超跌反弹。`,
    }
  }
  if (ad < 0.8 && trin > 1.1) {
    return {
      icon: '🛡️',
      tone: 'down',
      title: '空头占优 / 资金持续流出（防守阶段）',
      desc: `下跌家数明显多于上涨家数，且单日 TRIN (${trin.toFixed(2)}) 位于 1.0 上方但尚未达到 2.0 极值出清区，说明属于阴跌或空头趋势宣泄中途，建议控制仓位等待极值或宽度企稳。`,
    }
  }
  return {
    icon: '⚖️',
    tone: 'flat',
    title: '多空均衡拉锯（结构性分化震荡）',
    desc: `当前涨跌比 (${ad.toFixed(2)}) 与单日 TRIN (${trin.toFixed(2)}) 均处于中性平衡区间，市场缺乏单边增量资金驱动，以板块轮动与存量博弈为主。`,
  }
}

function armsPathNote(diverged, thrust) {
  const bits = []
  if (diverged?.kind === 'top') bits.push('这段里指数在高位，上涨占比却从高点收窄了')
  if (diverged?.kind === 'bottom') bits.push('指数在这段低点，累计涨跌家数没有再创新低')
  if (diverged?.kind === 'sync') bits.push('指数和腾落线一起停在这段高位')
  if (thrust?.from) {
    bits.push(
      `${String(thrust.from.date).slice(5)} 到 ${String(thrust.to.date).slice(5)} 上涨占比从 ${thrust.from.up_ratio.toFixed(1)}% 拉到 ${thrust.to.up_ratio.toFixed(1)}%`,
    )
  }
  return bits.length ? bits.join('。') + '。' : ''
}

function armsThrust(points) {
  const n = points.length
  for (let end = n - 1; end >= Math.max(0, n - 2); end -= 1) {
    const high = points[end].up_ratio
    if (high == null || high <= 70) continue
    for (let start = Math.max(0, end - 10); start < end; start += 1) {
      const low = points[start].up_ratio
      if (low != null && low < 30) return { from: points[start], to: points[end] }
    }
  }
  if (n >= 20) {
    const recent = points.slice(-10)
    const ratios = recent
      .map((point) => (point.decliners > 0 ? point.advancers / point.decliners : null))
      .filter((value) => value != null)
    const priorMin = Math.min(
      ...points
        .slice(-20, -10)
        .map((point) => point.up_ratio)
        .filter((value) => value != null && Number.isFinite(value)),
    )
    if (ratios.length >= 8 && Number.isFinite(priorMin) && priorMin < 30) {
      const avg = ratios.reduce((sum, value) => sum + value, 0) / ratios.length
      if (avg >= 1.9) return { ma: avg }
    }
  }
  return null
}

function armsDivergence(points, closes) {
  const rows = points
    .map((point, index) => ({ ...point, close: closes[index] }))
    .filter((row) => row.close != null && row.up_ratio != null)
  if (rows.length < 8) return { kind: null }
  const last = rows[rows.length - 1]
  const maxClose = Math.max(...rows.map((row) => row.close))
  const minClose = Math.min(...rows.map((row) => row.close))
  let net = 0
  const line = rows.map((row) => {
    net += row.advancers - row.decliners
    return net
  })
  const lastNet = line[line.length - 1]
  const maxNet = Math.max(...line)
  const minNet = Math.min(...line)
  const peak = Math.max(...rows.map((row) => row.up_ratio))
  const peakAt = rows.findIndex((row) => row.up_ratio === peak)
  const atHigh = last.close >= maxClose * 0.997
  const atLow = last.close <= minClose * 1.003
  const faded = peakAt <= rows.length - 4 && last.up_ratio <= peak - 15
  if (atHigh && faded) return { kind: 'top', peak, now: last.up_ratio }
  if (atLow && lastNet > minNet && line.indexOf(minNet) <= rows.length - 3) return { kind: 'bottom' }
  if (atHigh && lastNet >= maxNet && last.up_ratio >= peak - 3) return { kind: 'sync' }
  return { kind: null }
}

function armsVerdict(input) {
  const { adRatio, arms, upRatio, indexUp, diverged, thrust, position } = input
  const adText = adRatio == null ? '—' : adRatio.toFixed(2)
  const armsText = arms == null ? '—' : Number(arms).toFixed(2)
  const upText = upRatio == null ? '—' : Number(upRatio).toFixed(1)
  const indexText = indexUp == null ? '指数涨跌这次没对上' : indexUp ? '指数在涨' : '指数在跌'
  const extra = armsPathNote(diverged, thrust)
  const prev = input.previous
  if (
    prev?.up_ratio != null &&
    prev.up_ratio < 15 &&
    prev.arms != null &&
    prev.arms > 2.5 &&
    upRatio != null &&
    upRatio > prev.up_ratio
  ) {
    return {
      icon: '💎',
      tone: 'ice',
      title: '真见底：冰点之后宽度回升',
      desc: `${String(prev.date).slice(5)} 上涨占比 ${prev.up_ratio.toFixed(1)}%，阿姆士 ${Number(prev.arms).toFixed(2)}。今天回到 ${upText}%。这是当日上涨占比，不是站上 20 日均线的比例。${extra}`,
    }
  }
  if (upRatio != null && upRatio < 15 && arms != null && arms > 2.5) {
    return {
      icon: '💎',
      tone: 'ice',
      title: '真见底：恐慌盘带量涌出',
      desc: `当日上涨占比 ${upText}%，低于 15%，阿姆士 ${armsText}，高于 2.5。等下一个交易日宽度回升，这个左侧信号才算走出来。${extra}`,
    }
  }
  if (indexUp && adRatio > 2 && arms != null && arms < 0.8) {
    return {
      icon: '🚀',
      tone: 'up',
      title: '真突破：家数和成交一起偏向多头',
      desc: `${indexText}，涨跌家数比 ${adText}，大于 2，阿姆士 ${armsText}，小于 0.8。上涨的股票多，成交也堆在上涨的一边。${extra}`,
    }
  }
  if (indexUp && adRatio > 1.5 && arms != null && arms > 1.2) {
    return {
      icon: '⚠️',
      tone: 'hot',
      title: '假反弹：家数在涨，成交在下跌股里',
      desc: `${indexText}，涨跌家数比 ${adText}，大于 1.5，但阿姆士 ${armsText}，大于 1.2。看起来涨得多，下跌股票的平均成交更重。${extra}`,
    }
  }
  if (diverged?.kind === 'top') {
    return {
      icon: '⚠️',
      tone: 'hot',
      title: '虚胖：指数在高位，内部在变窄',
      desc: `指数停在这段高位，上涨占比从 ${diverged.peak.toFixed(1)}% 收到 ${upText}%。权重还撑着，多数股票已经不跟。`,
    }
  }
  if (diverged?.kind === 'bottom') {
    return {
      icon: '🛡️',
      tone: 'ice',
      title: '底背离：指数在低位，内部不再创新低',
      desc: '指数还在这段低点，累计涨跌家数已经不再跟着创新低。多数股票先止住了。',
    }
  }
  if (thrust?.from) {
    return {
      icon: '🚀',
      tone: 'up',
      title: '宽度冲力：十日内从冰点拉到普涨',
      desc: `${String(thrust.from.date).slice(5)} 上涨占比 ${thrust.from.up_ratio.toFixed(1)}%，${String(thrust.to.date).slice(5)} 升到 ${thrust.to.up_ratio.toFixed(1)}%。`,
    }
  }
  if (thrust?.ma) {
    return {
      icon: '🚀',
      tone: 'up',
      title: '宽度冲力：十日涨跌比抬过 1.9',
      desc: `近十个交易日涨跌家数比均值 ${thrust.ma.toFixed(2)}，再往前上涨占比曾经低于 30%。`,
    }
  }
  if (diverged?.kind === 'sync') {
    return {
      icon: '🚀',
      tone: 'up',
      title: '同步：指数和内部一起在高位',
      desc: `指数创这段新高时，上涨占比 ${upText}%，腾落线也在高位。上涨不是只靠少数权重。`,
    }
  }
  if (adRatio != null && adRatio < 0.8 && arms != null && arms > 1.1 && arms < 2) {
    return {
      icon: '🛡️',
      tone: 'down',
      title: '空头占优，还没到宣泄极值',
      desc: `涨跌家数比 ${adText}，阿姆士 ${armsText}。跌的更多，但还不是恐慌出清。`,
    }
  }
  return {
    icon: '⚖️',
    tone: 'flat',
    title: position ? `位置在${position.badge}，交叉还不极端` : '多空拉锯',
    desc: `${indexText}。涨跌家数比 ${adText}，阿姆士 ${armsText}，当日上涨占比 ${upText}%。没有同时落到真突破、假反弹或真见底。`,
  }
}

/** 涨跌均额比 = 上涨股平均成交 ÷ 下跌股平均成交（约等于 1/TRIN）。 */
function avgAmountRatioHint(ratio) {
  if (ratio == null || !Number.isFinite(Number(ratio))) return null
  const value = Number(ratio)
  if (value >= 2) return { tone: 'up', text: '涨侧极热' }
  if (value >= 1.3) return { tone: 'up', text: '涨侧占优' }
  if (value >= 0.85) return { tone: 'flat', text: '均额均衡' }
  if (value >= 0.5) return { tone: 'down', text: '跌侧占优' }
  return { tone: 'down', text: '跌侧极热' }
}

function buildArmsBoard(history, book, market, focusDate = null) {
  const stored = history?.points || []
  const live = book?.breadth || null
  const index =
    (live?.pair && (book?.indexes || []).find((item) => item.name === live.pair)) ||
    (book?.arms_index?.history?.length ? book.arms_index : null) ||
    (book?.indexes || []).find((item) => item.name === ARMS_INDEX[market]) ||
    book?.indexes?.[0] ||
    null
  const path = stored.map((point) => ({ ...point }))
  if (live && book?.as_of && live.arms != null) {
    const reading = {
      date: book.as_of,
      advancers: live.advancers,
      decliners: live.decliners,
      unchanged: live.unchanged,
      up_ratio: live.up_ratio,
      breadth: live.count ? ((live.advancers - live.decliners) / live.count) * 100 : null,
      arms: live.arms,
    }
    if (live.above_ma20 != null) reading.above_ma20 = live.above_ma20
    if (!path.length || path[path.length - 1].date < book.as_of) path.push(reading)
    else if (path[path.length - 1].date === book.as_of) Object.assign(path[path.length - 1], reading)
  }
  const latest =
    [...path].reverse().find((point) => point.arms != null || point.above_ma20 != null) ||
    path[path.length - 1]
  const focusKey = focusDate ? String(focusDate).slice(0, 10) : null
  const focused = focusKey
    ? path.find((point) => String(point.date).slice(0, 10) === focusKey)
    : null
  const source = focused || latest
  const viewingPast = Boolean(focused && latest && String(focused.date).slice(0, 10) !== String(latest.date).slice(0, 10))
  let headlineArms = source?.arms ?? null
  let headlineMa20 = source?.above_ma20 ?? null
  if (!viewingPast && source) {
    const earlier = [...path].reverse().find((point) => point.date < source.date)
    if (earlier) {
      if (headlineArms == null) headlineArms = earlier.arms
      if (headlineMa20 == null) headlineMa20 = earlier.above_ma20
    }
  }
  if (!source) {
    return { ready: false, plotted: [], indexName: index?.name || '', note: history?.note || '' }
  }
  const advancers = Number(source.advancers) || 0
  const decliners = Number(source.decliners) || 0
  const unchanged = Number(source.unchanged) || 0
  const total = advancers + decliners + unchanged
  const upRatio = source.up_ratio == null ? (total ? (advancers / total) * 100 : null) : Number(source.up_ratio)
  const arms = headlineArms == null ? null : Number(headlineArms)
  const adRatio = decliners > 0 ? advancers / decliners : null
  const volRatio = adRatio != null && arms > 0 ? adRatio / arms : null
  const intensity = arms > 0 ? 1 / arms : null
  const closesByDate = new Map((index?.history || []).map((point) => [point.date, point.close]))
  const plotted = path.map((point) => ({ ...point, indexClose: closesByDate.get(point.date) ?? null }))
  const focusIndex = plotted.findIndex((point) => String(point.date).slice(0, 10) === String(source.date).slice(0, 10))
  let indexUp = index?.day_change == null ? null : Number(index.day_change) > 0
  if (viewingPast && focusIndex >= 0) {
    const prev = focusIndex > 0 ? plotted[focusIndex - 1] : null
    const close = plotted[focusIndex]?.indexClose
    indexUp =
      close != null && prev?.indexClose != null ? Number(close) > Number(prev.indexClose) : null
  }
  const ma20 = headlineMa20 == null ? null : Number(headlineMa20)
  const position = armsPosition(ma20)
  // Badge, gauge, and panel verdict all use the same-session TRIN print.
  const verdict = armsPanelVerdict(adRatio, arms, ma20)
  const net = advancers - decliners
  const dayKey = String(source.date).slice(0, 10)
  const preferredIndex =
    ARMS_PAIR_INDEX[live?.key || history?.book || ''] || index?.name || ARMS_INDEX[market] || ''
  return {
    ready: true,
    key: live?.key || history?.book || '',
    universe: live?.universe || history?.universe || '',
    count: viewingPast ? total : live?.count || total,
    basis: history?.basis || (market === 'us' ? '成交量' : '成交额'),
    note: history?.note || '',
    indexName: preferredIndex || index?.name || '',
    indexUp,
    advancers,
    decliners,
    unchanged,
    total,
    pctAdv: total ? (advancers / total) * 100 : 0,
    pctUnch: total ? (unchanged / total) * 100 : 0,
    pctDec: total ? (decliners / total) * 100 : 0,
    net,
    upRatio,
    aboveMa20: headlineMa20 == null ? null : Number(headlineMa20),
    ma20: headlineMa20 == null ? null : Number(headlineMa20),
    adRatio,
    volRatio,
    arms,
    avgAmountRatio: intensity,
    avgAmountHint: avgAmountRatioHint(intensity),
    intensity,
    position,
    trin: armsTrinBadge(arms),
    verdict,
    plotted,
    trinLeft: armsPointer(arms),
    focusDate: viewingPast ? dayKey : null,
    focusLabel: viewingPast ? dayKey.slice(5) : null,
    focusIndex: viewingPast ? focusIndex : -1,
  }
}

const armsNarrowHistory = ref(null)

const armsBoard = computed(() =>
  buildArmsBoard(
    armsHistory.value,
    sentiment.value ? { ...sentiment.value, breadth: selectedBook.value } : null,
    store.market,
    armsFocusDate.value,
  ),
)

const armsNarrowBoard = computed(() => {
  const narrow = selectedBook.value?.narrow
  if (!narrow) return null
  return buildArmsBoard(
    armsNarrowHistory.value,
    sentiment.value ? { ...sentiment.value, breadth: narrow } : { breadth: narrow },
    store.market,
    armsFocusDate.value,
  )
})

/** Active modal board: full market by default; CSI 300 when toggled. */
const armsDisplayBoard = computed(() => {
  if (selectedBook.value?.key === 'cn_all' && armsScope.value === 'hs300' && armsNarrowBoard.value?.ready) {
    return armsNarrowBoard.value
  }
  return armsBoard.value
})

const armsVerdictBoard = computed(() => armsDisplayBoard.value)

const armsCompareColumns = computed(() => {
  const board = armsDisplayBoard.value
  if (!board?.ready) return []
  return [
    {
      title:
        armsScope.value === 'hs300' && selectedBook.value?.key === 'cn_all'
          ? '沪深300'
          : bookName(selectedBook.value) || board.universe || '全市场',
      board,
      tone: 'full',
    },
  ]
})

function setArmsScope(scope) {
  if (armsScope.value === scope) return
  armsScope.value = scope
  armsFocusDate.value = null
  nextTick(() => renderArmsChart())
}

function armsPointer(arms) {
  const clamped = Math.min(Math.max(arms ?? 1, 0.2), 3)
  return `${(((clamped - 0.2) / 2.8) * 100).toFixed(1)}%`
}

function armsTrinMa5Series(values) {
  return values.map((_, index) => {
    if (index < 4) return null
    const slice = values.slice(index - 4, index + 1).filter((value) => value != null && Number.isFinite(Number(value)))
    if (slice.length < 5) return null
    return Math.round((slice.reduce((sum, value) => sum + Number(value), 0) / 5) * 100) / 100
  })
}

function armsIndexClosesByName(name, dates) {
  const indexes = sentiment.value?.indexes || []
  const arms = sentiment.value?.arms_index
  const item =
    indexes.find((row) => row.name === name) || (arms?.name === name ? arms : null) || null
  const byDate = new Map((item?.history || []).map((point) => [String(point.date).slice(0, 10), Number(point.close)]))
  return dates.map((day) => {
    const value = byDate.get(String(day).slice(0, 10))
    return value == null || !Number.isFinite(value) ? null : value
  })
}

/** Rebase absolute index levels onto a shared 100 = first print in the window. */
function armsRebaseSeries(values) {
  const base = values.find((value) => value != null && value !== 0)
  if (base == null) return values.map(() => null)
  return values.map((value) => (value == null ? null : Math.round((value / base) * 1000) / 10))
}

const ARMS_SSE_COLOR = '#ef4444'
const ARMS_HS300_COLOR = '#f59e0b'
const ARMS_DJIA_COLOR = '#ef4444'
const ARMS_SPX_COLOR = '#f59e0b'
const ARMS_NDX_COLOR = '#3b82f6'
const ARMS_FULL_MA20_COLOR = '#38bdf8'
const ARMS_FULL_MA5_COLOR = '#64748b'

const armsChartMeta = computed(() => {
  const isCnAll = selectedBook.value?.key === 'cn_all'
  const isUsAll = selectedBook.value?.key === 'us_all'
  const fullView = isCnAll && armsScope.value === 'full'
  const hs300View = isCnAll && armsScope.value === 'hs300'
  const indexName = armsDisplayBoard.value?.indexName || '大盘指数'
  if (isUsAll) {
    return {
      mode: 'us_full',
      title: '历史 20 日时序背离与共振分析（主图：道琼斯/标普500/纳指100 + 全市场 %AboveMA20 | 副图：全市场 TRIN + MA5）',
      hint: '主图三指数各以窗口内首个有效值为100，便于比较走势；纳斯达克用纳指100。副图为全市场单日 TRIN 柱与 MA5。',
    }
  }
  if (fullView) {
    return {
      mode: 'full',
      title: '历史 20 日时序背离与共振分析（主图：上证/沪深300 + 全市场 %AboveMA20 | 副图：全市场 TRIN + MA5）',
      hint: '主图仅全市场高于MA20；上证与沪深300各以窗口内首个有效值为100，便于比较走势。副图为全市场单日 TRIN 柱与 MA5。',
    }
  }
  if (hs300View) {
    return {
      mode: 'hs300',
      title: '历史 20 日时序背离与共振分析（主图：上证/沪深300 + 沪深300 %AboveMA20 | 副图：沪深300 TRIN + MA5）',
      hint: '主图上证与沪深300各以窗口内首个有效值为100，便于比较走势；宽度与 TRIN 均为沪深300，副图含 MA5。',
    }
  }
  return {
    mode: 'single',
    title: `历史 20 日时序背离与共振分析（主图：${indexName} vs % Above MA20 | 副图：单日 TRIN + MA5）`,
    hint: '紫色柱代表单日 TRIN ≥ 2.0；灰虚线为 TRIN MA5。鼠标移到某一天，上方读数和结论会跟着变。',
  }
})

function renderArmsChart(retry = 0) {
  const board = armsDisplayBoard.value
  const points = board.plotted || []
  const host = armsChartRef.value?.isConnected
    ? armsChartRef.value
    : document.querySelector('.arms-modal .arms-chart')
  if (!host || !points.length) {
    if (retry < 8) requestAnimationFrame(() => renderArmsChart(retry + 1))
    else if (!points.length) disposeArmsChart()
    return
  }
  if (!armsChart || armsChart.getDom() !== host) {
    disposeArmsChart()
    armsChart = echarts.init(host)
  }
  const tone = trendPalette()
  const indexLabel = board.indexName || '大盘指数'
  const labels = points.map((point) => String(point.date).slice(5))
  const dayKeys = points.map((point) => String(point.date).slice(0, 10))
  const todayKey = new Intl.DateTimeFormat('en-CA', {
    timeZone: store.market === 'us' ? 'America/New_York' : store.market === 'hk' ? 'Asia/Hong_Kong' : 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
  const isCnAll = selectedBook.value?.key === 'cn_all'
  const isUsAll = selectedBook.value?.key === 'us_all' || board.key === 'us_all'
  const fullView = isCnAll && armsScope.value === 'full'
  const hs300View = isCnAll && armsScope.value === 'hs300'
  const cnDualIndexes = fullView || hs300View
  const usTripleIndexes = isUsAll
  const multiIndexes = cnDualIndexes || usTripleIndexes
  const trinSeries = points.map((point) => (point.arms == null ? null : Number(point.arms)))
  const barSeries = trinSeries
  const trinMa5 = armsTrinMa5Series(trinSeries)
  const sseRaw = cnDualIndexes ? armsIndexClosesByName('上证指数', dayKeys) : []
  const hs300Raw = cnDualIndexes ? armsIndexClosesByName('沪深300', dayKeys) : []
  const sseSeries = cnDualIndexes ? armsRebaseSeries(sseRaw) : []
  const hs300Series = cnDualIndexes ? armsRebaseSeries(hs300Raw) : []
  const djiaRaw = usTripleIndexes ? armsIndexClosesByName('道琼斯', dayKeys) : []
  const spxRaw = usTripleIndexes ? armsIndexClosesByName('标普 500', dayKeys) : []
  const ndxRaw = usTripleIndexes ? armsIndexClosesByName('纳斯达克 100', dayKeys) : []
  const djiaSeries = usTripleIndexes ? armsRebaseSeries(djiaRaw) : []
  const spxSeries = usTripleIndexes ? armsRebaseSeries(spxRaw) : []
  const ndxSeries = usTripleIndexes ? armsRebaseSeries(ndxRaw) : []
  const peakArms = Math.max(
    3,
    ...barSeries.filter((value) => value != null),
    ...trinMa5.filter((value) => value != null),
  )
  const narrow = viewportWidth.value <= 800
  const side = narrow ? 34 : 52
  const labelSize = narrow ? 10 : 11
  const focusIndex = board.focusIndex >= 0 ? board.focusIndex : -1
  const ma20Label = fullView || isUsAll
    ? '全市场 高于MA20(%)'
    : hs300View
      ? '沪深300 高于MA20(%)'
      : '高于MA20占比(%)'
  const trinBarLabel = fullView || isUsAll
    ? '全市场 单日 TRIN'
    : hs300View
      ? '沪深300 单日 TRIN'
      : '单日 TRIN'
  const trinMa5Label = fullView || isUsAll
    ? '全市场 TRIN MA5'
    : hs300View
      ? '沪深300 TRIN MA5'
      : 'TRIN MA5'
  const indexSeries = usTripleIndexes
    ? [
        {
          name: '道琼斯',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 1,
          data: djiaSeries,
          smooth: true,
          showSymbol: false,
          lineStyle: { color: ARMS_DJIA_COLOR, width: 2 },
          itemStyle: { color: ARMS_DJIA_COLOR },
        },
        {
          name: '标普 500',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 1,
          data: spxSeries,
          smooth: true,
          showSymbol: false,
          lineStyle: { color: ARMS_SPX_COLOR, width: 2 },
          itemStyle: { color: ARMS_SPX_COLOR },
        },
        {
          name: '纳斯达克 100',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 1,
          data: ndxSeries,
          smooth: true,
          showSymbol: false,
          lineStyle: { color: ARMS_NDX_COLOR, width: 2 },
          itemStyle: { color: ARMS_NDX_COLOR },
        },
      ]
    : cnDualIndexes
      ? [
          {
            name: '上证指数',
            type: 'line',
            xAxisIndex: 0,
            yAxisIndex: 1,
            data: sseSeries,
            smooth: true,
            showSymbol: false,
            lineStyle: { color: ARMS_SSE_COLOR, width: 2 },
            itemStyle: { color: ARMS_SSE_COLOR },
          },
          {
            name: '沪深300',
            type: 'line',
            xAxisIndex: 0,
            yAxisIndex: 1,
            data: hs300Series,
            smooth: true,
            showSymbol: false,
            lineStyle: { color: ARMS_HS300_COLOR, width: 2 },
            itemStyle: { color: ARMS_HS300_COLOR },
          },
        ]
      : [
          {
            name: indexLabel,
            type: 'line',
            xAxisIndex: 0,
            yAxisIndex: 1,
            data: points.map((point) => point.indexClose),
            smooth: true,
            showSymbol: false,
            lineStyle: { color: '#fbbf24', width: 2 },
            itemStyle: { color: '#fbbf24' },
          },
        ]
  const trinExtraSeries = [
    {
      name: trinMa5Label,
      type: 'line',
      xAxisIndex: 1,
      yAxisIndex: 2,
      data: trinMa5,
      smooth: true,
      showSymbol: false,
      lineStyle: { color: ARMS_FULL_MA5_COLOR, width: 1.5, type: 'dotted' },
      itemStyle: { color: ARMS_FULL_MA5_COLOR },
    },
  ]
  armsChart.setOption(
    {
      backgroundColor: 'transparent',
      animationDuration: 280,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis',
        backgroundColor: tone.paper,
        borderColor: tone.line,
        borderWidth: 1,
        padding: 10,
        extraCssText: 'border-radius:8px;box-shadow:0 8px 24px rgba(15, 23, 42, 0.12);',
        textStyle: { color: tone.ink, fontSize: 12 },
        formatter: (params) => {
          const index = params[0]?.dataIndex
          const point = points[index]
          if (!point) return ''
          const day = String(point.date).slice(0, 10)
          const title = day === todayKey ? `${day.slice(5)}(今)` : day.slice(5)
          const above = point.above_ma20 == null ? '—' : Number(point.above_ma20).toFixed(1)
          const barValue = barSeries[index]
          const trinColor = barValue == null ? '#94a3b8' : barValue >= 2 ? '#a855f7' : barValue <= 1 ? '#10b981' : '#ef4444'
          const line = (color, name, value, hollow = false) =>
            `<div style="display:flex;justify-content:space-between;gap:22px;align-items:center;color:#3d4452;font-size:12px;line-height:1.85"><span style="display:flex;align-items:center;gap:6px"><i style="width:8px;height:8px;border-radius:50%;box-sizing:border-box;border:2px solid ${color};background:${hollow ? 'transparent' : color}"></i>${name}</span><b style="font-weight:650;font-variant-numeric:tabular-nums;color:#131722">${value}</b></div>`
          const rows = [`<div style="color:#131722;font-size:13px;font-weight:650;margin-bottom:2px">${title}</div>`]
          rows.push(line(ARMS_FULL_MA20_COLOR, ma20Label, above))
          const fmt = (raw, rebased) =>
            raw == null ? '—' : `（${rebased == null ? '—' : rebased.toFixed(1)}） ${Number(raw).toLocaleString('en-US', { maximumFractionDigits: 2 })}`
          if (usTripleIndexes) {
            rows.push(line(ARMS_DJIA_COLOR, '道琼斯', fmt(djiaRaw[index], djiaSeries[index])))
            rows.push(line(ARMS_SPX_COLOR, '标普 500', fmt(spxRaw[index], spxSeries[index])))
            rows.push(line(ARMS_NDX_COLOR, '纳斯达克 100', fmt(ndxRaw[index], ndxSeries[index])))
          } else if (cnDualIndexes) {
            rows.push(line(ARMS_SSE_COLOR, '上证指数', fmt(sseRaw[index], sseSeries[index])))
            rows.push(line(ARMS_HS300_COLOR, '沪深300', fmt(hs300Raw[index], hs300Series[index])))
          } else {
            const close =
              point.indexClose == null
                ? '—'
                : Number(point.indexClose).toLocaleString('en-US', { maximumFractionDigits: 2 })
            rows.push(line('#fbbf24', indexLabel, close))
          }
          rows.push(line(trinColor, trinBarLabel, barValue == null ? '—' : barValue.toFixed(2)))
          rows.push(
            line(ARMS_FULL_MA5_COLOR, trinMa5Label, trinMa5[index] == null ? '—' : trinMa5[index].toFixed(2), true),
          )
          return rows.join('')
        },
      },
      grid: [
        { left: side, right: side, top: narrow ? 22 : 28, height: '42%' },
        { left: side, right: side, top: '62%', height: narrow ? '26%' : '28%' },
      ],
      xAxis: [
        {
          type: 'category',
          data: labels,
          gridIndex: 0,
          axisLabel: { show: false },
          axisTick: { show: false },
          axisLine: { lineStyle: { color: tone.line } },
        },
        {
          type: 'category',
          data: labels,
          gridIndex: 1,
          axisTick: { show: false },
          axisLine: { lineStyle: { color: tone.line } },
          axisLabel: { color: tone.axis, fontSize: labelSize, interval: narrow ? 3 : 'auto', showMinLabel: true, showMaxLabel: true },
        },
      ],
      yAxis: [
        {
          name: '% Above MA20',
          gridIndex: 0,
          min: 0,
          max: 100,
          interval: 25,
          axisLabel: { color: '#38bdf8', fontSize: labelSize, formatter: '{value}%' },
          nameTextStyle: { color: '#38bdf8', fontSize: labelSize },
          splitLine: { lineStyle: { color: tone.split, type: 'dashed' } },
        },
        {
          name: multiIndexes ? '指数' : indexLabel,
          gridIndex: 0,
          scale: true,
          position: 'right',
          axisLabel: {
            color: usTripleIndexes ? ARMS_NDX_COLOR : cnDualIndexes ? ARMS_HS300_COLOR : '#fbbf24',
            fontSize: labelSize,
          },
          nameTextStyle: {
            color: usTripleIndexes ? ARMS_NDX_COLOR : cnDualIndexes ? ARMS_HS300_COLOR : '#fbbf24',
            fontSize: labelSize,
          },
          splitLine: { show: false },
        },
        {
          name: fullView || isUsAll ? '全市场 TRIN' : hs300View ? '沪深300 TRIN' : 'TRIN (阿姆士)',
          gridIndex: 1,
          min: 0,
          max: Math.ceil(peakArms * 10) / 10,
          axisLabel: { color: tone.axis, fontSize: labelSize },
          nameTextStyle: { color: tone.axis, fontSize: labelSize },
          splitLine: { lineStyle: { color: tone.split, type: 'dashed' } },
        },
      ],
      series: [
        {
          name: ma20Label,
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: points.map((point) => point.above_ma20),
          smooth: true,
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { color: ARMS_FULL_MA20_COLOR, width: 2 },
          itemStyle: { color: ARMS_FULL_MA20_COLOR },
          areaStyle: multiIndexes
            ? undefined
            : {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: 'rgba(56, 189, 248, 0.35)' },
                  { offset: 1, color: 'rgba(56, 189, 248, 0.02)' },
                ]),
              },
          markLine: {
            symbol: 'none',
            silent: true,
            data: [
              { yAxis: 80, lineStyle: { color: '#f59e0b', type: 'dashed', width: 1 }, label: { formatter: '80% 超买区', color: '#f59e0b', fontSize: 10, position: 'insideEndTop' } },
              { yAxis: 20, lineStyle: { color: '#c084fc', type: 'dashed', width: 1 }, label: { formatter: '20% 冰点底', color: '#c084fc', fontSize: 10, position: 'insideEndBottom' } },
              ...(focusIndex >= 0
                ? [{ xAxis: labels[focusIndex], lineStyle: { color: tone.axis, type: 'dashed', width: 1.2 }, label: { show: false } }]
                : []),
            ],
          },
        },
        ...indexSeries,
        {
          name: trinBarLabel,
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: barSeries,
          barMaxWidth: 16,
          itemStyle: {
            borderRadius: [3, 3, 0, 0],
            color: (params) => {
              const value = barSeries[params.dataIndex]
              if (value == null) return '#64748b'
              if (value >= 2) return '#a855f7'
              return value <= 1 ? ARMS_UP : ARMS_DOWN
            },
          },
          markLine: {
            symbol: 'none',
            silent: true,
            data: [
              { yAxis: 1, lineStyle: { color: '#94a3b8', width: 1 }, label: { formatter: '1.0 多空均衡', color: tone.axis, fontSize: 10, position: narrow ? 'insideStartTop' : 'insideEndTop' } },
              { yAxis: 2, lineStyle: { color: '#a855f7', type: 'dashed', width: 1 }, label: { formatter: '2.0 恐慌超卖线', color: '#7c3aed', fontSize: 10, position: narrow ? 'insideStartTop' : 'insideEndTop' } },
              ...(focusIndex >= 0
                ? [{ xAxis: labels[focusIndex], lineStyle: { color: tone.axis, type: 'dashed', width: 1.2 }, label: { show: false } }]
                : []),
            ],
          },
        },
        ...trinExtraSeries,
      ],
    },
    true,
  )
  armsChart.off('updateAxisPointer')
  armsChart.off('globalout')
  armsChart.on('updateAxisPointer', (event) => {
    const fromSeries = event?.seriesDataIndices?.find((item) => item?.dataIndex != null)
    let index = fromSeries?.dataIndex
    if (index == null) {
      const axis = (event?.axesInfo || []).find((item) => item?.value != null)
      index = axis?.value
      if (typeof index === 'string') index = labels.indexOf(index)
    }
    if (index == null || index < 0 || index >= points.length) return
    const point = points[index]
    if (!point?.date) return
    const day = String(point.date).slice(0, 10)
    const last = String(points[points.length - 1]?.date || '').slice(0, 10)
    const next = day === last ? null : day
    if (armsFocusDate.value === next) return
    armsFocusDate.value = next
  })
  armsChart.on('globalout', () => {
    if (armsFocusDate.value != null) armsFocusDate.value = null
  })
  armsChart.resize()
}

async function selectBreadthBook(key) {
  breadthBook.value = key
  await openArmsHistory()
}

function openIndexBook(name) {
  const key = INDEX_BOOK[name]
  if (key) selectBreadthBook(key)
}

async function openArmsHistory() {
  armsOpen.value = true
  armsScope.value = 'full'
  armsFocusDate.value = null
  armsLoading.value = true
  armsHistory.value = null
  armsNarrowHistory.value = null
  try {
    armsHistory.value = await fetchBreadthHistory(breadthBook.value)
    const narrowKey = selectedBook.value?.narrow_key
    if (narrowKey) {
      try {
        armsNarrowHistory.value = await fetchBreadthHistory(narrowKey)
      } catch {
        armsNarrowHistory.value = null
      }
    }
  } catch {
    armsHistory.value = null
  } finally {
    armsLoading.value = false
  }
  await nextTick()
  renderArmsChart()
}

watch(armsOpen, (open) => {
  if (!open) {
    armsFocusDate.value = null
    disposeArmsChart()
  }
})

watch(
  () => [
    armsHistory.value,
    armsNarrowHistory.value,
    sentiment.value?.as_of,
    selectedBook.value?.arms,
    selectedBook.value?.key,
    selectedBook.value?.narrow?.arms,
    armsScope.value,
    armsOpen.value,
    armsLoading.value,
  ],
  () => {
    if (!armsOpen.value || armsLoading.value) return
    nextTick(() => renderArmsChart())
  },
)

let sentimentBusy = false
let sentimentTries = 0
let maTries = 0

async function loadSentiment({ silent = false } = {}) {
  if (sentimentBusy) return
  sentimentBusy = true
  if (!silent) {
    sentimentTries = 0
    sentimentState.value = 'loading'
  }
  try {
    sentiment.value = await fetchMarketSentiment()
    sentimentState.value = 'ready'
    await nextTick()
    renderSentimentChart()
  } catch {
    if (!silent) {
      sentiment.value = null
      sentimentState.value = 'error'
    }
  } finally {
    sentimentBusy = false
  }
  const books = sentiment.value?.books || []
  const waitingMa = books.some((book) => book.ma_pending && book.ma_total == null)
  if (store.market !== 'hk' && !books.length && sentimentTries < 8) {
    sentimentTries += 1
    window.setTimeout(() => loadSentiment({ silent: true }), 15000)
  } else if (waitingMa && maTries < 16) {
    maTries += 1
    window.setTimeout(() => loadSentiment({ silent: true }), 20000)
  } else if (books.length) {
    sentimentTries = 0
    if (!waitingMa) maTries = 0
  }
}

function trendPalette() {
  const dark = document.documentElement.dataset.theme === 'dark'
  return {
    ink: dark ? '#d1d4dc' : '#243047',
    axis: dark ? '#868b96' : '#8b95a8',
    line: dark ? '#363a45' : '#e7ebf2',
    split: dark ? '#2a2e39' : '#eef2f6',
    slider: dark ? '#2a2e39' : '#f6f8fb',
    handle: dark ? '#1e222d' : '#fff',
    handleBorder: dark ? '#363a45' : '#d5deea',
    paper: dark ? '#1e222d' : '#fff',
  }
}

function chartSlider(dates, extra = {}) {
  const tone = trendPalette()
  return {
    type: 'slider',
    height: 18,
    bottom: 2,
    backgroundColor: tone.slider,
    borderColor: 'transparent',
    fillerColor: 'rgba(20, 151, 125, 0.14)',
    handleSize: 14,
    handleStyle: { color: tone.handle, borderColor: tone.handleBorder, borderWidth: 1 },
    moveHandleSize: 0,
    brushSelect: false,
    showDetail: false,
    dataBackground: {
      lineStyle: { color: tone.axis, width: 1 },
      areaStyle: { color: 'rgba(20, 151, 125, 0.08)' },
    },
    selectedDataBackground: {
      lineStyle: { color: '#14977d', width: 1 },
      areaStyle: { color: 'rgba(20, 151, 125, 0.16)' },
    },
    textStyle: { color: tone.axis, fontSize: 10 },
    labelFormatter: (value) => (typeof value === 'string' ? value.slice(5) : dates[value]?.slice(5) || ''),
    ...extra,
  }
}

const SENTIMENT_COLORS = ['#14977d', '#3b6fd8', '#d9852a', '#7c5cbf']

function renderSentimentChart({ keepRange = false } = {}) {
  const book = sentiment.value
  if (!sentimentChartRef.value || !book) return
  const indexes = (book.indexes || []).filter((item) => item.history?.length)
  const anchor =
    indexes.find((item) => item.name === '纳斯达克 100' || item.name === '纳斯达克') || indexes[0]
  const points = anchor?.history?.length ? anchor.history : book.vix?.history || []
  if (!points.length) return
  if (!sentimentChart) sentimentChart = echarts.init(sentimentChartRef.value)
  const tone = trendPalette()
  const previousZoom = keepRange ? sentimentChart.getOption()?.dataZoom?.[0] : null
  const dates = points.map((point) => point.date)
  const zoomStart = dates[Math.max(0, dates.length - 60)]
  const zoomEnd = dates[dates.length - 1]
  const zoomRange = previousZoom
    ? { start: previousZoom.start, end: previousZoom.end }
    : { startValue: zoomStart, endValue: zoomEnd }
  const axisLabel = { color: tone.axis, fontSize: 11 }
  const series = indexes.map((item, index) => {
    const byDate = Object.fromEntries(item.history.map((point) => [point.date, point.close]))
    const base = item.history.find((point) => point.close)?.close
    const color = SENTIMENT_COLORS[index % SENTIMENT_COLORS.length]
    return {
      name: item.name === '纳斯达克 100' || item.name === '纳斯达克' ? '纳指100' : item.name,
      type: 'line',
      smooth: 0.18,
      showSymbol: false,
      symbol: 'circle',
      symbolSize: 7,
      data: dates.map((date) => {
        const close = byDate[date]
        if (close == null || !base) return null
        return { value: Math.round((close / base) * 1000) / 10, close }
      }),
      lineStyle: { width: index === 0 ? 2.2 : 1.6, color, cap: 'round', join: 'round' },
      itemStyle: { color, borderColor: tone.paper, borderWidth: 2 },
      emphasis: { focus: 'series' },
    }
  })
  if (book.vix?.history?.length) {
    const vixByDate = Object.fromEntries(book.vix.history.map((point) => [point.date, point.close]))
    series.push({
      name: 'VIX',
      type: 'line',
      yAxisIndex: 1,
      smooth: 0.18,
      showSymbol: false,
      symbol: 'circle',
      symbolSize: 7,
      data: dates.map((date) => vixByDate[date] ?? null),
      lineStyle: { width: 1.4, color: tone.axis, cap: 'round', join: 'round' },
      itemStyle: { color: tone.axis, borderColor: tone.paper, borderWidth: 2 },
      emphasis: { focus: 'series' },
    })
  }
  sentimentChart.setOption(
    {
      animationDuration: 280,
      grid: { left: 4, right: 4, top: 32, bottom: 28, containLabel: true },
      tooltip: {
        trigger: 'axis',
        backgroundColor: tone.paper,
        borderColor: tone.line,
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: tone.ink, fontSize: 12 },
        axisPointer: { type: 'line', lineStyle: { color: tone.axis, type: 'dashed', width: 1 } },
        formatter: (params) => {
          const rows = params.map((item) => {
            const close = item.data?.close
            const text = close == null ? (item.data ?? '—') : close
            return `${item.marker}${item.seriesName} ${text}`
          })
          return `${params[0]?.axisValue || ''}<br/>${rows.join('<br/>')}`
        },
      },
      legend: {
        top: 0,
        right: 0,
        type: 'scroll',
        icon: 'circle',
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 12,
        textStyle: { color: tone.axis, fontSize: 12 },
      },
      dataZoom: [
        {
          type: 'inside',
          zoomOnMouseWheel: true,
          moveOnMouseMove: true,
          moveOnMouseWheel: false,
          ...zoomRange,
        },
        chartSlider(dates, zoomRange),
      ],
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: tone.line } },
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
          name: '起点100',
          nameTextStyle: { color: tone.axis, fontSize: 11 },
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: tone.split } },
          axisLabel,
        },
        {
          type: 'value',
          scale: true,
          show: Boolean(book.vix?.history?.length),
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

function openTrend(symbol = '') {
  const symbols = trendEligibleSymbols.value
  if (!symbols.length) return
  const selected = symbols.includes(symbol)
    ? symbol
    : symbols.includes(trendSymbol.value) ? trendSymbol.value : symbols[0]
  trendOpen.value = true
  if (trendSymbol.value !== selected) {
    trendSymbol.value = selected
    return
  }
  loadTrend()
}

function trendPointChange(point) {
  return point?.day_change_percent ?? null
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
  if (!trendEligibleSymbols.value.includes(symbol)) return
  const token = ++trendLoadToken
  trendPaging = false
  trendLoading.value = true
  trendCursor.value = null
  // Drop the previous symbol's book and canvas up front: keeping them would show the
  // old stock's chart under the new name while loading, and on failure forever.
  trendBook.value = null
  if (trendChart) trendChart.clear()
  try {
    const page = await fetchIndicatorHistory(symbol, { limit: TREND_PAGE })
    if (token !== trendLoadToken || trendSymbol.value !== symbol || !trendOpen.value) return
    trendBook.value = page
    trendCursor.value = page.points?.[page.points.length - 1] || null
    await nextTick()
    renderTrendChart()
  } catch {
    // A chart error must not throw away rows that already arrived.
    if (token === trendLoadToken && trendBook.value?.symbol !== symbol) trendBook.value = null
  } finally {
    if (token === trendLoadToken) trendLoading.value = false
  }
}

async function loadOlderTrend() {
  const book = trendBook.value
  const symbol = trendSymbol.value
  if (!book?.has_more || !book.before || trendPaging || !trendEligibleSymbols.value.includes(symbol)) return
  const token = trendLoadToken
  trendPaging = true
  try {
    const page = await fetchIndicatorHistory(symbol, { before: book.before, limit: TREND_PAGE })
    if (token !== trendLoadToken || trendSymbol.value !== symbol || !trendOpen.value) return
    if (!page.points?.length) {
      trendBook.value = { ...book, has_more: false }
      return
    }
    trendBook.value = {
      ...book,
      points: [...page.points, ...(book.points || [])],
      has_more: page.has_more,
      before: page.before,
    }
    await nextTick()
    renderTrendChart({ keepRange: true })
  } catch {
    // The next slide to the older edge can retry.
  } finally {
    trendPaging = false
  }
}

const THS_LINE = {
  ma5: '#8B93A3',
  ma10: '#7C5CBF',
  ma20: '#F07A1A',
  bollUp: '#F23645',
  bollMid: '#1A4F9C',
  bollLow: '#089981',
  dif: '#2B2F36',
  dea: '#F0B400',
}

function trendStudyGraphics(point) {
  const macd = point?.macd
  const ink = trendPalette().ink
  const macdColor = macd == null ? ink : macd >= 0 ? '#089981' : '#f23645'
  const piece = (fill) => ({ fill, fontSize: 11 })
  return [
    {
      id: 'boll-caption',
      type: 'text',
      silent: true,
      left: 0,
      top: '49%',
      style: {
        text: `{name|布林}  {up|上轨} {v|${adviceNum(point?.boll_upper)}}  {mid|中轨} {v|${adviceNum(point?.ma20)}}  {low|下轨} {v|${adviceNum(point?.boll_lower)}}`,
        rich: {
          name: { ...piece(ink), fontWeight: 650 },
          up: piece(THS_LINE.bollUp),
          mid: piece(THS_LINE.bollMid),
          low: piece(THS_LINE.bollLow),
          v: piece(ink),
        },
      },
    },
    {
      id: 'macd-caption',
      type: 'text',
      silent: true,
      left: 0,
      top: '64.8%',
      style: {
        text: `{name|MACD}  {dif|DIF} {v|${adviceNum(point?.macd_dif, 4)}}  {dea|DEA} {v|${adviceNum(point?.macd_dea, 4)}}  {bar|MACD} {m|${adviceNum(macd, 4)}}`,
        rich: {
          name: { ...piece(THS_LINE.dif), fontWeight: 650 },
          dif: piece(THS_LINE.dif),
          dea: piece(THS_LINE.dea),
          bar: piece(macdColor),
          v: piece(ink),
          m: piece(macdColor),
        },
      },
    },
  ]
}

const TREND_RULE_ORDER = [
  'volatility_compression_breakout_v1',
  'ma_crossover_v1',
  'rsi_reversion_v1',
  'mean_reversion_oversold_v1',
]
const TREND_BUY = '#2962ff'
const TREND_SELL = '#f23645'

function trendAdviceMarks(points) {
  // The server walks one position per symbol from a fixed first day with the backtest
  // engine's entry and exit checks, so the marks do not move when older bars load.
  const marks = []
  points.forEach((point, index) => {
    for (const item of point.advice || []) {
      const byRule = item.kind === 'rule'
      marks.push({
        index,
        side: item.side,
        title: item.side === 'B' ? 'B 建议买入' : byRule ? 'S 卖出规则' : 'S 持仓离场',
        reason: byRule ? item.rule : item.side === 'S' && item.rule ? `${item.rule}：${item.reason}` : item.reason,
      })
    }
  })
  return marks
}

function trendTooltip(point, prevClose, dayMarks) {
  if (!point) return ''
  const tone = trendPalette()
  const close = Number(point.close)
  const prev = Number(prevClose)
  const change = prev > 0 ? ((close - prev) / prev) * 100 : null
  const changeColor = change == null ? tone.ink : change >= 0 ? '#089981' : '#f23645'
  const ma = (label, value, color) =>
    value == null
      ? ''
      : `<span style="margin-right:10px"><i style="display:inline-block;width:8px;height:2px;background:${color};margin-right:4px;vertical-align:middle"></i>${label} ${adviceNum(value)}</span>`
  const row = (label, ok) =>
    `<div style="font-size:11px;line-height:1.6"><span style="color:${ok ? '#089981' : '#f23645'};font-weight:700">${ok ? '✓' : '✗'}</span> ${label}</div>`
  const heading = (text) =>
    `<div style="margin-top:2px;font-size:10px;font-weight:700;color:${tone.axis}">${text}</div>`
  let html = `<div style="font-size:12px;line-height:1.75;max-width:100%;overflow-wrap:anywhere;color:${tone.ink}">`
  html += `<div style="font-weight:650;margin-bottom:2px">${point.date}</div>`
  html += `<div>开 ${adviceNum(point.open)} · 高 ${adviceNum(point.high)} · 低 ${adviceNum(point.low)}</div>`
  html += `<div>收 <b style="color:${changeColor}">${adviceNum(point.close)}</b>`
  html += change == null ? '' : ` <span style="color:${changeColor}">${formatPct(change)}</span>`
  html += ` · 量 ${formatCount(point.volume)}</div>`
  html += `<div style="margin-top:2px">${ma('MA5', point.ma5, THS_LINE.ma5)}${ma('MA10', point.ma10, THS_LINE.ma10)}${ma('MA20', point.ma20, THS_LINE.ma20)}</div>`
  for (const mark of dayMarks) {
    const buy = mark.side === 'B'
    const color = buy ? TREND_BUY : TREND_SELL
    html += `<div style="margin-top:5px;padding-top:5px;border-top:1px dashed ${tone.line}"><b style="color:${color}">${mark.title}</b> ${adviceNum(point.close)}`
    html += `<div style="color:${tone.axis};font-size:11px">${mark.reason}</div></div>`
  }
  const buys = [...(point.strategies || [])].sort(
    (a, b) => TREND_RULE_ORDER.indexOf(a.id) - TREND_RULE_ORDER.indexOf(b.id),
  )
  for (const [title, list] of [
    ['买入规则条件', buys],
    ['卖出规则条件', point.sells || []],
  ]) {
    const rules = list.filter((rule) => (rule.core || []).some((item) => item.ok))
    if (!rules.length) continue
    html += `<div style="margin-top:6px;padding-top:6px;border-top:1px dashed ${tone.line}">`
    html += `<div style="font-weight:650">${title}</div>`
    for (const rule of rules) {
      const core = rule.core || []
      const optional = rule.optional || []
      const coreHit = core.filter((item) => item.ok).length
      html += `<div style="margin-top:5px;font-weight:650">${rule.name} <span style="color:${tone.axis};font-weight:400;font-size:10px">${rule.ready ? '全部满足' : `必填 ${coreHit}/${core.length} · 辅助 ${rule.opt_hits}/${rule.need}`}</span></div>`
      if (core.length) {
        html += heading('必填：')
        html += core.map((item) => row(item.label, item.ok)).join('')
        html += heading(`辅助：至少 ${rule.need} 项`)
        html += optional.map((item) => row(item.label, item.ok)).join('')
      } else if (rule.detail) {
        html += `<div style="font-size:11px">${rule.detail}</div>`
      }
    }
    html += '</div>'
  }
  html += '</div>'
  return html
}

function priceWindow(highs, lows, start, end) {
  let hi = -Infinity
  let lo = Infinity
  for (let index = start; index <= end; index += 1) {
    const high = Number(highs[index])
    const low = Number(lows[index])
    if (Number.isFinite(high)) hi = Math.max(hi, high)
    if (Number.isFinite(low)) lo = Math.min(lo, low)
  }
  if (!Number.isFinite(hi) || !Number.isFinite(lo)) return { min: null, max: null, hi: null, lo: null }
  const span = hi - lo || Math.abs(hi) * 0.02 || 1
  const pad = span * 0.08
  return { min: lo - pad, max: hi + pad, hi, lo }
}

function zoomIndexes(zoom, categories) {
  const last = Math.max(0, categories.length - 1)
  let start = 0
  let end = last
  if (zoom?.startValue != null) {
    const index = categories.indexOf(zoom.startValue)
    if (index >= 0) start = index
  } else if (typeof zoom?.start === 'number') {
    start = Math.round((zoom.start / 100) * last)
  }
  if (zoom?.endValue != null) {
    const index = categories.indexOf(zoom.endValue)
    if (index >= 0) end = index
  } else if (typeof zoom?.end === 'number') {
    end = Math.round((zoom.end / 100) * last)
  }
  return [Math.max(0, Math.min(start, end)), Math.min(last, Math.max(start, end))]
}

function placePriceTags(chart, hi, lo, text, color) {
  const highPixel = chart.convertToPixel({ yAxisIndex: 0 }, hi)
  const lowPixel = chart.convertToPixel({ yAxisIndex: 0 }, lo)
  const highY = Array.isArray(highPixel) ? highPixel[1] : highPixel
  const lowY = Array.isArray(lowPixel) ? lowPixel[1] : lowPixel
  if (!Number.isFinite(highY) || !Number.isFinite(lowY)) return
  const tag = (id, value, top) => ({
    id,
    type: 'text',
    silent: true,
    z: 30,
    left: 2,
    top,
    style: { text: text(value), fill: color, fontSize: 11 },
  })
  chart.setOption({
    graphic: [tag('k-high', hi, highY - 14), tag('k-low', lo, lowY + 2)],
  })
}

let trendTagging = false

function syncTrendPriceTags() {
  if (!trendChart || trendTagging) return
  const rows = trendBook.value?.points || []
  if (!rows.length) return
  const option = trendChart.getOption()
  const categories = option.xAxis?.[0]?.data || rows.map((point) => point.date)
  const [start, end] = zoomIndexes(option.dataZoom?.[0], categories)
  const span = priceWindow(
    rows.map((point) => point.high),
    rows.map((point) => point.low),
    start,
    end,
  )
  if (span.hi == null) return
  trendTagging = true
  trendChart.setOption({ yAxis: [{ min: span.min, max: span.max }] })
  trendTagging = false
  requestAnimationFrame(() => {
    if (!trendChart) return
    placePriceTags(trendChart, span.hi, span.lo, (value) => adviceNum(value), trendPalette().axis)
  })
}

function renderTrendChart({ keepRange = false } = {}) {
  const points = trendBook.value?.points || []
  if (!trendChartRef.value) return
  if (!points.length) {
    // No data for this symbol: never leave the previous symbol's candles on screen.
    if (trendChart) trendChart.clear()
    return
  }
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
  const tone = trendPalette()
  const marks = []
  points.forEach((point, index) => {
    if (point.signal === 'squeeze') {
      marks.push({
        coord: [dates[index], point.low],
        symbol: 'arrow',
        symbolRotate: 0,
        symbolOffset: [0, 10],
        itemStyle: { color: upColor },
        label: { formatter: '缩量', position: 'bottom', color: upColor, fontSize: 10 },
      })
    } else if (point.signal === 'distribute') {
      marks.push({
        coord: [dates[index], point.high],
        symbol: 'arrow',
        symbolRotate: 180,
        symbolOffset: [0, -10],
        itemStyle: { color: '#e6a700' },
        label: { formatter: '滞涨', position: 'top', color: '#8a6400', fontSize: 10 },
      })
    }
  })
  const adviceMarks = trendAdviceMarks(points)
  const renderAdviceMark = (params, api) => {
    const mark = adviceMarks[params.dataIndex]
    if (!mark) return null
    const buy = mark.side === 'B'
    const color = buy ? TREND_BUY : TREND_SELL
    const center = api.coord([api.value(0), api.value(1)])
    const half = Math.max(api.size([1, 0])[0] / 2, 4)
    const cx = center[0]
    const cy = center[1]
    return {
      type: 'group',
      silent: true,
      children: [
        {
          type: 'line',
          shape: { x1: cx - half * 0.94, y1: cy, x2: cx + half * 0.94, y2: cy },
          style: { stroke: color, lineWidth: 2, lineCap: 'round', lineDash: buy ? undefined : [5, 3], opacity: 0.95 },
        },
        {
          type: 'circle',
          shape: { cx, cy, r: 6.5 },
          style: { fill: color, stroke: tone.paper, lineWidth: 2 },
        },
        {
          type: 'text',
          style: {
            text: mark.side,
            x: cx,
            y: cy,
            fill: '#fff',
            fontSize: 9,
            fontWeight: 700,
            align: 'center',
            verticalAlign: 'middle',
          },
        },
      ],
    }
  }
  const mom = points[points.length - 1]?.mom
  const wash = mom > 0 ? 'rgba(8,153,129,0.08)' : mom < 0 ? 'rgba(230,167,0,0.10)' : 'transparent'
  const zoomEnd = dates[dates.length - 1]
  const zoomStart = kept?.startValue && dates.includes(kept.startValue)
    ? kept.startValue
    : dates[Math.max(0, dates.length - TREND_PAGE)]
  const zoomStop = kept?.endValue && dates.includes(kept.endValue) ? kept.endValue : zoomEnd
  const priceSpan = priceWindow(
    points.map((point) => point.high),
    points.map((point) => point.low),
    Math.max(0, dates.indexOf(zoomStart)),
    Math.max(0, dates.indexOf(zoomStop)),
  )
  trendZoomGuard = { start: zoomStart, end: zoomStop }
  const zoomStamp = ++trendZoomStamp
  trendZoomLive = false
  let lastTrendTip = ''
  trendChart.setOption(
    {
      animationDuration: 280,
      axisPointer: {
        link: [{ xAxisIndex: [0, 1, 2, 3, 4] }],
        lineStyle: { color: tone.line, type: 'dashed', width: 1 },
      },
      legend: { show: false },
      tooltip: {
        show: true,
        trigger: 'axis',
        // Fixed band: top sits on the Bollinger caption (just under the candles
        // and volume) and the bottom stops above the zoom slider. ECharts rewrites
        // the tooltip style on every move, so the size is applied again each time.
        confine: false,
        enterable: true,
        hideDelay: 400,
        transitionDuration: 0,
        position(_point, _params, dom, _rect, size) {
          const [viewWidth, viewHeight] = size.viewSize
          const top = Math.round(viewHeight * 0.49)
          const height = Math.max(80, viewHeight - top - 24)
          const width = Math.min(Math.round(viewWidth * 0.7), viewWidth)
          if (dom) {
            const style = dom.style
            style.boxSizing = 'border-box'
            style.width = `${width}px`
            style.height = `${height}px`
            style.minHeight = `${height}px`
            style.maxHeight = `${height}px`
            style.maxWidth = '70vw'
            style.overflowX = 'hidden'
            style.overflowY = 'auto'
            style.whiteSpace = 'normal'
            style.pointerEvents = 'auto'
          }
          return [0, top]
        },
        backgroundColor: tone.paper,
        borderColor: tone.line,
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: tone.ink, fontSize: 12 },
        extraCssText: 'box-shadow:0 8px 24px rgba(20,30,55,.12);border-radius:10px;overflow-wrap:anywhere;',
        axisPointer: { type: 'cross', lineStyle: { color: tone.line, type: 'dashed', width: 1 } },
        formatter: (params) => {
          const list = Array.isArray(params) ? params : [params]
          const hit = list.find((item) => Number.isInteger(item?.dataIndex))
          let index = hit?.dataIndex
          if (!Number.isInteger(index)) {
            const date = list[0]?.axisValue ?? list[0]?.name
            index = typeof date === 'number' ? date : points.findIndex((item) => item.date === date)
          }
          if (!points[index]) return lastTrendTip
          const dayMarks = adviceMarks.filter((mark) => mark.index === index)
          lastTrendTip = trendTooltip(points[index], index > 0 ? points[index - 1].close : null, dayMarks)
          return lastTrendTip
        },
      },
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: [0, 1, 2, 3, 4],
          zoomOnMouseWheel: false,
          moveOnMouseMove: false,
          moveOnMouseWheel: false,
          preventDefaultMouseMove: true,
          startValue: zoomStart,
          endValue: zoomStop,
        },
        chartSlider(dates, {
          xAxisIndex: [0, 1, 2, 3, 4],
          left: 0,
          right: 0,
          startValue: zoomStart,
          endValue: zoomStop,
        }),
      ],
      grid: [
        { left: 0, right: 0, top: 8, bottom: '58%' },
        { left: 0, right: 0, top: '43%', height: '5.5%' },
        { left: 0, right: 0, top: '53%', height: '11%' },
        { left: 0, right: 0, top: '69%', height: '12%' },
        { left: 0, right: 0, top: '84.5%', height: '5.5%' },
      ],
      graphic: trendStudyGraphics(points.find((point) => point.date === trendCursor.value?.date) || points[points.length - 1]),
      xAxis: [0, 1, 2, 3, 4].map((gridIndex) => ({
        type: 'category',
        gridIndex,
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: tone.line } },
        axisPointer: {
          label: { show: false },
        },
        axisLabel: {
          show: gridIndex === 4,
          color: tone.axis,
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
          min: priceSpan.min,
          max: priceSpan.max,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { show: false },
          axisPointer: { label: { show: false } },
          splitLine: { lineStyle: { color: tone.split } },
        },
        { gridIndex: 1, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
        { gridIndex: 2, scale: true, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
        { gridIndex: 3, scale: true, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
        { gridIndex: 4, scale: true, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
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
          markPoint: {
            symbolSize: 10,
            data: marks,
            tooltip: { show: false },
          },
          markArea: {
            silent: true,
            itemStyle: { color: wash },
            data: [[{ xAxis: dates[0] }, { xAxis: dates[dates.length - 1] }]],
          },
        },
        {
          name: '建议买卖',
          type: 'custom',
          xAxisIndex: 0,
          yAxisIndex: 0,
          coordinateSystem: 'cartesian2d',
          renderItem: renderAdviceMark,
          data: adviceMarks.map((mark) => [mark.index, points[mark.index].close]),
          silent: true,
          clip: true,
          z: 12,
          tooltip: { show: false },
        },
        {
          name: 'MA5',
          type: 'line',
          showSymbol: false,
          smooth: 0.12,
          data: points.map((point) => point.ma5),
          lineStyle: { width: 1.2, color: THS_LINE.ma5, cap: 'round', join: 'round' },
          itemStyle: { color: THS_LINE.ma5 },
        },
        {
          name: 'MA10',
          type: 'line',
          showSymbol: false,
          smooth: 0.12,
          data: points.map((point) => point.ma10),
          lineStyle: { width: 1.2, color: THS_LINE.ma10, cap: 'round', join: 'round' },
          itemStyle: { color: THS_LINE.ma10 },
        },
        {
          name: 'MA20',
          type: 'line',
          showSymbol: false,
          smooth: 0.12,
          data: points.map((point) => point.ma20),
          lineStyle: { width: 1.8, color: THS_LINE.ma20, cap: 'round', join: 'round' },
          itemStyle: { color: THS_LINE.ma20 },
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
          xAxisIndex: 4,
          yAxisIndex: 4,
          data: points.map((point) => ({
            value: point.illiq,
            itemStyle: {
              color:
                point.signal === 'squeeze'
                  ? upColor
                  : point.signal === 'distribute'
                    ? '#e6a700'
                    : point.signal === 'shrink_down'
                      ? downColor
                      : '#c5cad3',
            },
          })),
        },
        {
          name: '布林K线',
          type: 'candlestick',
          xAxisIndex: 2,
          yAxisIndex: 2,
          data: candles,
          itemStyle: {
            color: upColor,
            color0: downColor,
            borderColor: upColor,
            borderColor0: downColor,
          },
        },
        {
          name: '布林上轨',
          type: 'line',
          xAxisIndex: 2,
          yAxisIndex: 2,
          showSymbol: false,
          smooth: 0.12,
          data: points.map((point) => point.boll_upper),
          lineStyle: { width: 1.2, color: THS_LINE.bollUp },
          itemStyle: { color: THS_LINE.bollUp },
        },
        {
          name: '布林中轨',
          type: 'line',
          xAxisIndex: 2,
          yAxisIndex: 2,
          showSymbol: false,
          smooth: 0.12,
          data: points.map((point) => point.ma20),
          lineStyle: { width: 1.2, color: THS_LINE.bollMid },
          itemStyle: { color: THS_LINE.bollMid },
        },
        {
          name: '布林下轨',
          type: 'line',
          xAxisIndex: 2,
          yAxisIndex: 2,
          showSymbol: false,
          smooth: 0.12,
          data: points.map((point) => point.boll_lower),
          lineStyle: { width: 1.2, color: THS_LINE.bollLow },
          itemStyle: { color: THS_LINE.bollLow },
        },
        {
          name: 'MACD',
          type: 'bar',
          xAxisIndex: 3,
          yAxisIndex: 3,
          data: points.map((point) => ({
            value: point.macd,
            itemStyle: { color: (point.macd ?? 0) >= 0 ? upColor : downColor },
          })),
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { color: tone.line, width: 1 },
            data: [{ yAxis: 0 }],
          },
        },
        {
          name: 'DIF',
          type: 'line',
          xAxisIndex: 3,
          yAxisIndex: 3,
          showSymbol: false,
          data: points.map((point) => point.macd_dif),
          lineStyle: { width: 1.2, color: THS_LINE.dif },
          itemStyle: { color: THS_LINE.dif },
        },
        {
          name: 'DEA',
          type: 'line',
          xAxisIndex: 3,
          yAxisIndex: 3,
          showSymbol: false,
          data: points.map((point) => point.macd_dea),
          lineStyle: { width: 1.2, color: THS_LINE.dea },
          itemStyle: { color: THS_LINE.dea },
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
    if (point) {
      trendCursor.value = point
      trendChart.setOption({ graphic: trendStudyGraphics(point) })
    }
  })
  trendChart.off('globalout')
  trendChart.on('globalout', () => {
    const latest = points[points.length - 1] || null
    trendCursor.value = latest
    if (latest) trendChart.setOption({ graphic: trendStudyGraphics(latest) })
  })
  trendChart.off('datazoom')
  trendChart.on('datazoom', () => {
    syncTrendPriceTags()
    if (!trendZoomLive || trendPaging) return
    const dates = (trendBook.value?.points || []).map((point) => point.date)
    const range = captureTrendRange(dates)
    if (!range) return
    if (trendZoomGuard && range.startValue === trendZoomGuard.start && range.endValue === trendZoomGuard.end) return
    trendZoomGuard = null
    if (trendSliderNearOldest(dates, range.startValue)) loadOlderTrend()
  })
  bindTrendSlider(trendChartRef.value)
  trendChart.resize()
  requestAnimationFrame(() => syncTrendPriceTags())
  setTimeout(() => {
    if (zoomStamp === trendZoomStamp) trendZoomLive = true
  }, 80)
}

function trendSliderNearOldest(dates, startValue) {
  const index = dates.indexOf(startValue)
  return index >= 0 && index <= 2
}

function bindTrendSlider(el) {
  if (!el || el.dataset.trendSlider) return
  el.dataset.trendSlider = '1'
  let origin = null
  const onSlider = (event) => event.offsetY >= el.clientHeight - 28
  el.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || !onSlider(event)) {
      origin = null
      return
    }
    origin = { x: event.clientX }
  })
  el.addEventListener('pointerup', (event) => {
    if (!origin || !trendZoomLive) {
      origin = null
      return
    }
    const dx = event.clientX - origin.x
    origin = null
    if (dx > -24) return
    const dates = (trendBook.value?.points || []).map((point) => point.date)
    const range = captureTrendRange(dates)
    if (range && trendSliderNearOldest(dates, range.startValue)) loadOlderTrend()
  })
  el.addEventListener('pointercancel', () => {
    origin = null
  })
}

function trendCell(record, key) {
  const value = record[key]
  if (key === 'ma_order' || key === 'stop_candle' || key === 'anchor_date') return value || '—'
  if (key === 'volume' || key === 'turnover') return formatCount(value)
  if (key === 'illiq') return adviceNum(value, 3)
  if (key === 'day_change_percent') return value == null ? '—' : formatPct(value)
  if (['vs_ma20', 'vs_anchor', 'erp'].includes(key)) return adviceSignedPct(value)
  if (['bandwidth', 'vol', 'vol_cap', 'rp_weight'].includes(key)) return advicePct(value)
  if (key === 'mom') return adviceNum(value, 2)
  if (key === 'rsi') return adviceNum(value, 1)
  return adviceNum(value)
}

function trendTone(record, key) {
  if (key === 'ma_order') {
    const order = record[key]
    return order === '多头' ? 'pnl-up' : order === '空头' ? 'pnl-down' : ''
  }
  if (!['vs_ma20', 'vs_anchor', 'mom', 'day_change', 'day_change_percent', 'erp'].includes(key) || record[key] == null) return ''
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
  signals: (r) => imageSignals(r),
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

function onViewportResize() {
  viewportWidth.value = window.innerWidth
  priceChart?.resize()
  sentimentChart?.resize()
  armsChart?.resize()
  trendChart?.resize()
  if (priceModalOpen.value) renderPriceChart()
  if (armsOpen.value) renderArmsChart()
}

watch(subTab, () => {
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
watch(isDark, () => {
  if (trendOpen.value && trendChart) renderTrendChart({ keepRange: true })
  if (sentimentChart) renderSentimentChart({ keepRange: true })
  if (armsOpen.value && armsChart) renderArmsChart()
})
watch(trendSymbol, () => {
  if (trendOpen.value) loadTrend()
})
watch(trendEligibleSymbols, (symbols) => {
  if (symbols.includes(trendSymbol.value)) return
  trendLoadToken += 1
  trendLoading.value = false
  trendPaging = false
  trendBook.value = null
  trendCursor.value = null
  if (trendChart) trendChart.clear()
  trendSymbol.value = trendOpen.value ? symbols[0] || '' : ''
  if (!symbols.length) trendOpen.value = false
})
watch(() => store.market, () => {
  trendOpen.value = false
  trendSymbol.value = ''
  trendBook.value = null
  trendCursor.value = null
})
watch(() => store.quoteTick, () => {
  autoRefreshQuotes()
  // Session-bar signals move intraday, but the advice call is expensive; refresh it
  // on a slower cadence than the 10s quote tick so it cannot starve other requests.
  const now = Date.now()
  if (isMarketOpen(store.market) && now - lastIndicatorPoll >= INDICATOR_POLL_MS) {
    lastIndicatorPoll = now
    loadIndicators({ silent: true })
  }
  loadSentiment({ silent: true })
})
onMounted(() => {
  load()
  loadTrendScope()
  loadSentiment()
  loadCnDesk()
  loadCnFlow()
  window.addEventListener('resize', onViewportResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', onViewportResize)
  disposePriceChart()
  disposeSentimentChart()
  disposeTrendChart()
})
</script>

<template>
  <div class="holdings-view">
    <section class="market-hero">
      <div class="market-hero-copy">
        <p class="market-kicker"><span class="market-kicker-dot"></span>市场情绪</p>
        <h2 v-if="sentiment">{{ sentiment.label }}</h2>
        <h2 v-else-if="sentimentState !== 'error'">读取中</h2>
        <h2 v-else>暂时没有取到</h2>
        <p class="market-hero-note">
          {{ sentiment?.note || (sentimentState === 'error' ? '市场情绪暂时没有取到。' : '正在读取涨跌家数和指数。') }}
        </p>
        <div v-if="sentiment" class="market-hero-meta">
          <span v-if="sentiment.as_of">截至 {{ sentiment.as_of }}</span>
          <span v-if="sentiment?.breadth">{{ sentiment?.breadth.universe }} · {{ sentiment?.breadth.count }} 只</span>
          <span v-if="sentiment?.breadth">平盘 {{ sentiment?.breadth.unchanged }}</span>
          <span v-if="sentiment?.breadth?.strong_up != null">涨超 2% {{ sentiment?.breadth.strong_up }}</span>
          <span v-if="sentiment?.breadth?.strong_down != null">跌超 2% {{ sentiment?.breadth.strong_down }}</span>
        </div>
      </div>
      <div class="market-hero-side">
      <div v-if="sentimentBooks.length" class="breadth-books">
        <button
          v-for="book in sentimentBooks"
          :key="book.key"
          type="button"
          class="breadth-book"
          :class="{ 'is-on': book.key === breadthBook }"
          @click="selectBreadthBook(book.key)"
        >
          <span class="breadth-book-head">
            <span class="breadth-book-name">{{ bookName(book) }}</span>
            <span v-if="book.pair" class="breadth-book-pair">对照 {{ book.pair }}</span>
          </span>
          <span class="breadth-book-count">{{ book.count }} 家</span>
          <span v-if="book.ma_total != null" class="breadth-book-sample" :title="book.above_ma20 == null ? '拿到前 19 日收盘价的只数未满当场成交的九成，高于 MA20 的比例先不写' : '拿到前 19 日收盘价的只数 / 当场有成交的只数'">
            日线 {{ book.ma_valid || 0 }}/{{ book.ma_total }} · {{ Math.round((book.ma_valid || 0) / book.ma_total * 100) }}%
            <template v-if="book.above_ma20 != null"> · 高于MA20 {{ Number(book.above_ma20).toFixed(1) }}%</template>
          </span>
          <span class="breadth-book-stats">
            <span>
              <span class="breadth-book-label">
                <span class="market-metric-icon teal"><RiseOutlined /></span>
                <em>上涨</em>
              </span>
              <strong class="pnl-up">{{ book.advancers }}</strong>
            </span>
            <span>
              <span class="breadth-book-label">
                <span class="market-metric-icon amber"><FallOutlined /></span>
                <em>下跌</em>
              </span>
              <strong class="pnl-down">{{ book.decliners }}</strong>
            </span>
            <span>
              <span class="breadth-book-label">
                <span class="market-metric-icon blue"><LineChartOutlined /></span>
                <em>宽度</em>
              </span>
              <strong :class="pnlClass(breadthWidth(book))">
                {{ breadthWidth(book) == null ? '—' : `${breadthWidth(book) > 0 ? '+' : ''}${breadthWidth(book).toFixed(1)}%` }}
              </strong>
            </span>
            <span>
              <span class="breadth-book-label">
                <span class="market-metric-icon violet"><DashboardOutlined /></span>
                <em>阿姆士</em>
              </span>
              <strong :class="book.arms == null ? '' : book.arms < 1 ? 'pnl-up' : book.arms > 1 ? 'pnl-down' : ''">
                {{ book.arms == null ? '—' : book.arms.toFixed(2) }}
              </strong>
            </span>
          </span>
        </button>
      </div>
      <div
        v-if="(!sentimentBooks.length && (sentiment?.breadth || (sentiment && store.market !== 'hk'))) || (store.market === 'cn' && cnDesk?.flow)"
        class="market-metrics"
      >
        <template v-if="sentiment?.breadth && !sentimentBooks.length">
        <article>
          <a-tooltip v-if="store.market === 'cn' && upBuckets.length" placement="bottomLeft">
            <template #title>
              <div v-for="bucket in upBuckets" :key="bucket.label">{{ bucket.label }} {{ bucket.count }}</div>
            </template>
            <div class="metric-hit">
              <span class="market-metric-icon teal"><RiseOutlined /></span>
              <div>
                <p>上涨</p>
                <strong class="pnl-up">{{ sentiment?.breadth.advancers }}</strong>
                <span>涨停 {{ cnDesk?.activity?.limit_up ?? '—' }}</span>
              </div>
            </div>
          </a-tooltip>
          <div v-else class="metric-hit">
            <span class="market-metric-icon teal"><RiseOutlined /></span>
            <div>
              <p>上涨</p>
              <strong class="pnl-up">{{ sentiment?.breadth.advancers }}</strong>
              <span v-if="store.market === 'cn'">涨停 {{ cnDesk?.activity?.limit_up ?? '—' }}</span>
              <span v-else>当前行情里的上涨家数</span>
            </div>
          </div>
        </article>
        <article>
          <a-tooltip v-if="store.market === 'cn' && downBuckets.length" placement="bottomLeft">
            <template #title>
              <div v-for="bucket in downBuckets" :key="bucket.label">{{ bucket.label }} {{ bucket.count }}</div>
            </template>
            <div class="metric-hit">
              <span class="market-metric-icon amber"><FallOutlined /></span>
              <div>
                <p>下跌</p>
                <strong class="pnl-down">{{ sentiment?.breadth.decliners }}</strong>
                <span>跌停 {{ cnDesk?.activity?.limit_down ?? '—' }}</span>
              </div>
            </div>
          </a-tooltip>
          <div v-else class="metric-hit">
            <span class="market-metric-icon amber"><FallOutlined /></span>
            <div>
              <p>下跌</p>
              <strong class="pnl-down">{{ sentiment?.breadth.decliners }}</strong>
              <span v-if="store.market === 'cn'">跌停 {{ cnDesk?.activity?.limit_down ?? '—' }}</span>
              <span v-else>当前行情里的下跌家数</span>
            </div>
          </div>
        </article>
        <article>
          <span class="market-metric-icon blue"><LineChartOutlined /></span>
          <div>
            <p>上涨占比</p>
            <strong>{{ sentiment?.breadth.up_ratio == null ? '—' : `${sentiment?.breadth.up_ratio.toFixed(1)}%` }}</strong>
            <span>{{ sentiment?.breadth.universe }}里上涨所占的比例</span>
          </div>
        </article>
        <article v-if="breadthWidth(sentiment?.breadth) != null">
          <span class="market-metric-icon blue"><LineChartOutlined /></span>
          <div>
            <p>市场宽度</p>
            <strong :class="pnlClass(breadthWidth(sentiment?.breadth))">
              {{ `${breadthWidth(sentiment?.breadth) > 0 ? '+' : ''}${breadthWidth(sentiment?.breadth).toFixed(1)}%` }}
            </strong>
            <span>上涨家数减下跌家数，再除以总家数</span>
          </div>
        </article>
        <article v-if="sentiment?.breadth.median_change != null">
          <span class="market-metric-icon violet"><DashboardOutlined /></span>
          <div>
            <p>中位涨跌</p>
            <strong :class="pnlClass(sentiment?.breadth.median_change)">
              {{ `${sentiment?.breadth.median_change > 0 ? '+' : ''}${sentiment?.breadth.median_change.toFixed(2)}%` }}
            </strong>
            <span>{{ sentiment?.breadth.universe }}的中位数</span>
          </div>
        </article>
        </template>
        <article
          v-if="store.market !== 'hk' && !sentimentBooks.length"
          class="is-arms"
          title="查看宽广度和阿姆士"
          @click="openArmsHistory"
        >
          <span class="market-metric-icon violet"><DashboardOutlined /></span>
          <div>
            <p>阿姆士</p>
            <strong
              v-if="sentiment?.breadth?.arms == null"
            >—</strong>
            <strong
              v-else
              :class="sentiment?.breadth.arms < 1 ? 'pnl-up' : sentiment?.breadth.arms > 1 ? 'pnl-down' : ''"
            >
              {{ sentiment?.breadth.arms.toFixed(2) }}
            </strong>
            <span v-if="sentiment?.breadth?.arms == null">正在读取全市场，点开可看以前的</span>
            <span v-else>{{ store.market === 'us' ? '小于 1 表示上涨股票的成交量更大' : '小于 1 表示上涨股票的成交额更大' }}</span>
          </div>
        </article>
        <article v-if="store.market === 'cn' && cnDesk?.flow" class="is-main-flow">
          <a-tooltip placement="bottomLeft">
            <template #title>
              <div v-for="row in flowTiers" :key="row.label">{{ row.text }}</div>
            </template>
            <div class="metric-hit">
              <div>
                <p>大盘主力</p>
                <strong :class="pnlClass(cnDesk.flow.main)">{{ flowYi(cnDesk.flow.main) }}</strong>
                <span>占成交 {{ cnDesk.flow.main_ratio == null ? '—' : `${cnDesk.flow.main_ratio > 0 ? '+' : ''}${Number(cnDesk.flow.main_ratio).toFixed(2)}%` }}</span>
              </div>
            </div>
          </a-tooltip>
          <div class="main-flow-tiers">
            <span v-for="row in flowTiers.slice(1)" :key="row.label">
              <span class="breadth-book-label">
                <span class="market-metric-icon" :class="flowTone(row.label)"><LineChartOutlined /></span>
                <em>{{ row.label }}</em>
              </span>
              <strong :class="pnlClass(row.amount)">{{ flowYi(row.amount) }}</strong>
            </span>
          </div>
        </article>
      </div>
      </div>
      </section>
    <section v-if="store.market === 'cn' && cnFlow" class="market-tape">
      <div class="market-tape-row">
        <article>
          <p>融资余额<small v-if="cnFlow.margin_date">截至 {{ cnFlow.margin_date }}</small></p>
          <strong>{{ cnFlow.margin_balance == null ? '—' : `${Number(cnFlow.margin_balance).toFixed(2)} 万亿` }}</strong>
          <span>
            占流通市值 {{ cnFlow.margin_ratio == null ? '—' : `${Number(cnFlow.margin_ratio).toFixed(2)}%` }}
            · 近 20 个交易日 {{ cnFlow.margin_change_pct == null ? '—' : formatPct(cnFlow.margin_change_pct) }}
            · 近一年位置 {{ cnFlow.margin_rank == null ? '—' : `${Number(cnFlow.margin_rank).toFixed(0)}%` }}
          </span>
        </article>
        <article v-for="item in cnFlow.etfs" :key="item.code">
          <p>{{ item.name }}<small>份额 4 周</small></p>
          <strong :class="pnlClass(item.change_pct)">{{ item.change_pct == null ? '—' : formatPct(item.change_pct) }}</strong>
          <span>{{ item.shares == null ? '—' : `${Number(item.shares).toFixed(0)} 亿份` }}<template v-if="cnFlow.etf_base_date"> · 对比 {{ cnFlow.etf_base_date }}</template></span>
        </article>
      </div>
      <p class="sentiment-note">{{ cnFlow.note }}</p>
    </section>
    <section v-if="sentiment && (sentiment.vix || sentiment.indexes?.length)" class="market-tape">
      <div class="market-tape-row">
        <article v-if="sentiment.vix">
          <p>VIX</p>
          <strong>
            {{ sentiment.vix.last == null ? '—' : sentiment.vix.last.toFixed(2) }}
            <span :class="pnlClass(sentiment.vix.day_change)">
              {{ sentiment.vix.day_change == null ? '—' : `${sentiment.vix.day_change > 0 ? '+' : ''}${sentiment.vix.day_change.toFixed(2)}%` }}
            </span>
          </strong>
        </article>
        <article
          v-for="item in sentiment.indexes"
          :key="item.name"
          :class="{ 'is-linked': INDEX_BOOK[item.name] }"
          @click="openIndexBook(item.name)"
        >
          <p>
            {{ item.name }}
            <small v-if="indexCaption(item.name)">{{ indexCaption(item.name) }}</small>
          </p>
          <strong>
            {{ item.last == null ? '—' : item.last.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}
            <span :class="pnlClass(item.day_change)">
              {{ item.day_change == null ? '—' : `${item.day_change > 0 ? '+' : ''}${item.day_change.toFixed(2)}%` }}
            </span>
          </strong>
          <span>
            MA20 {{ item.ma20 == null ? '—' : item.ma20.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}
            <span :class="pnlClass(item.vs_ma20)">{{ item.vs_ma20 == null ? '—' : `${item.vs_ma20 > 0 ? '+' : ''}${item.vs_ma20.toFixed(2)}%` }}</span>
          </span>
        </article>
      </div>
      <div ref="sentimentChartRef" class="sentiment-chart"></div>
    </section>
    <a-tabs v-model:activeKey="subTab" size="small">
      <a-tab-pane key="positions" tab="持仓" />
      <a-tab-pane key="cumulative" tab="累计" />
      <a-tab-pane key="closed" tab="清仓" />
    </a-tabs>

    <template v-if="subTab === 'cumulative'">
      <div class="toolbar">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
        <div class="toolbar-actions">
          <a-button :disabled="!trendEligibleSymbols.length" @click="openTrend()">指标趋势</a-button>
          <a-button
            :loading="imageExporting"
            :disabled="!data.items.length"
            @click="handleDownloadImage('cumulative')"
          >
            <DownloadOutlined /> 下载图片
          </a-button>
        </div>
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

    <template v-else-if="subTab === 'closed'">
      <div class="toolbar">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
        <div class="toolbar-actions">
          <a-button :disabled="!trendEligibleSymbols.length" @click="openTrend()">指标趋势</a-button>
          <a-button
            :loading="imageExporting"
            :disabled="!data.items.length"
            @click="handleDownloadImage('closed')"
          >
            <DownloadOutlined /> 下载图片
          </a-button>
        </div>
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
      <div class="toolbar">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
        <div class="toolbar-actions">
          <a-button :disabled="!trendEligibleSymbols.length" @click="openTrend()">指标趋势</a-button>
          <a-button
            :loading="imageExporting"
            :disabled="!data.items.length"
            @click="handleDownloadImage('positions')"
          >
            <DownloadOutlined /> 下载图片
          </a-button>
        </div>
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
          <template v-else-if="column.key === 'signals'">
            <button
              type="button"
              class="signal-trigger"
              :disabled="!trendEligibleSymbols.includes(record.symbol)"
              :aria-label="`查看 ${record.name || tickerOf(record.symbol)} 指标趋势`"
              @click.stop="openTrend(record.symbol)"
            >
              <span v-if="!signalListOf(record)" class="signal-cell is-wait">{{ adviceLoading ? '…' : '—' }}</span>
              <span v-else-if="!signalListOf(record).length" class="signal-cell is-none">未触发</span>
              <span v-else class="signal-cell is-fired" :title="signalTitle(record)">
                <b v-for="item in signalListOf(record)" :key="item.id" class="signal-tag">{{ item.name }}</b>
              </span>
            </button>
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
          <template v-else-if="['cost_basis', 'market_value'].includes(column.key)">
            {{ record[column.key] == null ? '—' : formatMoney(record[column.key], record.currency || 'USD') }}
          </template>
          <template v-else-if="column.key === 'total_pnl'">
            <span :class="pnlClass(record.total_pnl)">{{ formatMoney(record.total_pnl) }}</span>
          </template>
          <template v-else-if="column.key === 'total_pnl_percent'">
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
      v-model:open="armsOpen"
      wrap-class-name="arms-modal"
      :width="viewportWidth > 800 ? 1120 : '100%'"
      centered
      destroy-on-close
      :footer="null"
    >
      <template #title>
        <div class="arms-modal-title-row">
          <span class="arms-modal-title">
            {{
              canToggleArmsScope && armsScope === 'hs300'
                ? '沪深300'
                : bookName(selectedBook) || selectedBook?.universe || '市场宽广度'
            }}
            · 阿姆士
          </span>
          <div v-if="canToggleArmsScope" class="arms-scope-toggle" role="tablist" aria-label="宽度范围">
            <button type="button" role="tab" :aria-selected="armsScope === 'full'" :class="{ 'is-on': armsScope === 'full' }" @click="setArmsScope('full')">
              全市场
            </button>
            <button
              type="button"
              role="tab"
              :aria-selected="armsScope === 'hs300'"
              :class="{ 'is-on': armsScope === 'hs300' }"
              :disabled="!armsNarrowBoard?.ready"
              @click="setArmsScope('hs300')"
            >
              沪深300
            </button>
          </div>
        </div>
      </template>
      <div v-if="armsLoading" class="arms-empty">正在读取最近 22 个交易日</div>
      <div v-else-if="!armsDisplayBoard.ready" class="arms-empty">这段行情还没有汇总出来</div>
      <div v-else class="arms-board">
        <div v-if="armsDisplayBoard.focusLabel" class="arms-focus-bar">
          <span>正在查看 {{ armsDisplayBoard.focusLabel }} 的宽广度和阿姆士</span>
        </div>
        <section class="arms-compare">
          <div v-for="col in armsCompareColumns" :key="col.tone" class="arms-compare-col" :class="`is-${col.tone}`">
            <section class="arms-cards">
              <article>
                <div class="arms-card-head">
                  <h3>市场宽广度 <small>(Market Breadth)</small></h3>
                  <span class="arms-tag">正向指标</span>
                  <em v-if="col.board.position" :class="`is-${col.board.position.tone}`">{{ col.board.position.badge }}</em>
                </div>
                <div class="arms-card-main">
                  <div>
                    <p>
                      高于 MA20 成分股占比 (% Above MA20)
                      <template v-if="col.board.focusLabel"> · {{ col.board.focusLabel }}</template>
                    </p>
                    <div class="arms-figure">
                      <strong>{{ col.board.ma20 == null ? '—' : `${col.board.ma20.toFixed(1)}%` }}</strong>
                      <span :class="col.board.net > 0 ? 'is-up' : col.board.net < 0 ? 'is-down' : ''">
                        净涨跌 (A-D): {{ col.board.net > 0 ? '+' : '' }}{{ col.board.net }}
                      </span>
                    </div>
                  </div>
                  <div class="is-side">
                    <p>涨跌家数比 (A/D Ratio)</p>
                    <b>{{ col.board.adRatio == null ? '—' : col.board.adRatio.toFixed(2) }}</b>
                  </div>
                </div>
                <div class="arms-split">
                  <span class="is-up">上涨 (A): {{ col.board.advancers }} ({{ col.board.pctAdv.toFixed(1) }}%)</span>
                  <span>平盘 (U): {{ col.board.unchanged }} ({{ col.board.pctUnch.toFixed(1) }}%)</span>
                  <span class="is-down">下跌 (D): {{ col.board.decliners }} ({{ col.board.pctDec.toFixed(1) }}%)</span>
                </div>
                <div class="arms-bar">
                  <i class="is-up" :style="{ width: `${col.board.pctAdv}%` }"></i>
                  <i class="is-flat" :style="{ width: `${col.board.pctUnch}%` }"></i>
                  <i class="is-down" :style="{ width: `${col.board.pctDec}%` }"></i>
                </div>
              </article>
              <article>
                <div class="arms-card-head">
                  <h3>阿姆士指数 <small>(Arms Index / TRIN)</small></h3>
                  <span class="arms-tag is-reverse">反向指标</span>
                  <em :class="`is-${col.board.trin.tone}`">{{ col.board.trin.text }}</em>
                </div>
                <div class="arms-card-main">
                  <div>
                    <p>
                      {{ col.board.focusLabel ? `${col.board.focusLabel} TRIN` : '实时 TRIN' }} = (A / D) ÷ (AV / DV)
                    </p>
                    <div class="arms-figure">
                      <strong>{{ col.board.arms == null ? '—' : col.board.arms.toFixed(2) }}</strong>
                      <span v-if="col.board.adRatio != null && col.board.volRatio != null">
                        涨跌比 {{ col.board.adRatio.toFixed(2) }} ÷ 量比 {{ col.board.volRatio.toFixed(2) }}
                      </span>
                    </div>
                  </div>
                  <div class="is-side">
                    <p>涨跌均额比</p>
                    <b :class="col.board.avgAmountHint?.tone === 'up' ? 'is-up' : col.board.avgAmountHint?.tone === 'down' ? 'is-down' : ''">
                      {{ col.board.avgAmountRatio == null ? '—' : col.board.avgAmountRatio.toFixed(2) }}
                    </b>
                    <em v-if="col.board.avgAmountHint" :class="`is-${col.board.avgAmountHint.tone}`">
                      {{ col.board.avgAmountHint.text }}
                    </em>
                  </div>
                </div>
                <div class="arms-gauge-scale">
                  <span>0.2 极度超买</span>
                  <span>0.6 多头强势</span>
                  <span>1.0 多空均衡</span>
                  <span>1.6 空头抛压</span>
                  <span class="is-panic">2.0+ 恐慌超卖底</span>
                </div>
                <div class="arms-gauge">
                  <i class="is-one"></i>
                  <i class="is-two"></i>
                  <b :style="{ left: col.board.trinLeft }"></b>
                </div>
                <div class="arms-ratio-scale">
                  <span>≥2.0 涨侧极热</span>
                  <span>≥1.3 涨侧占优</span>
                  <span>≈1.0 均额均衡</span>
                  <span>≤0.77 跌侧占优</span>
                  <span>≤0.5 跌侧极热</span>
                </div>
              </article>
            </section>
          </div>
        </section>
        <section v-if="armsVerdictBoard?.verdict" class="arms-synergy" :class="`is-${armsVerdictBoard.verdict.tone}`">
          <span class="arms-synergy-icon">{{ armsVerdictBoard.verdict.icon }}</span>
          <div>
            <p>
              宽广度 × 单日 TRIN 交叉验证结论：
              <strong>{{ armsVerdictBoard.verdict.title }}</strong>
            </p>
            <span>{{ armsVerdictBoard.verdict.desc }}</span>
          </div>
        </section>
        <section class="arms-chart-card">
          <div class="arms-chart-head">
            <div>
              <h3>{{ armsChartMeta.title }}</h3>
              <p>{{ armsChartMeta.hint }}</p>
            </div>
            <div class="arms-legend">
              <template v-if="armsChartMeta.mode === 'us_full'">
                <span><i class="is-ratio"></i>全市场 高于MA20</span>
                <span><i class="is-djia"></i>道琼斯</span>
                <span><i class="is-spx"></i>标普 500</span>
                <span><i class="is-ndx"></i>纳指100</span>
                <span><i class="is-panic"></i>全市场 TRIN 柱</span>
                <span><i class="is-full-ma5"></i>全市场 TRIN MA5</span>
              </template>
              <template v-else-if="armsChartMeta.mode === 'full'">
                <span><i class="is-ratio"></i>全市场 高于MA20</span>
                <span><i class="is-sse"></i>上证指数</span>
                <span><i class="is-hs300"></i>沪深300</span>
                <span><i class="is-panic"></i>全市场 TRIN 柱</span>
                <span><i class="is-full-ma5"></i>全市场 TRIN MA5</span>
              </template>
              <template v-else-if="armsChartMeta.mode === 'hs300'">
                <span><i class="is-ratio"></i>沪深300 高于MA20</span>
                <span><i class="is-sse"></i>上证指数</span>
                <span><i class="is-hs300"></i>沪深300</span>
                <span><i class="is-panic"></i>沪深300 TRIN 柱</span>
                <span><i class="is-full-ma5"></i>沪深300 TRIN MA5</span>
              </template>
              <template v-else>
                <span><i class="is-index"></i>{{ armsDisplayBoard.indexName || '大盘指数' }}</span>
                <span><i class="is-ratio"></i>% Above MA20</span>
                <span><i class="is-panic"></i>TRIN≥2.0 恐慌底</span>
                <span><i class="is-full-ma5"></i>TRIN MA5</span>
              </template>
            </div>
          </div>
          <div v-if="!armsDisplayBoard.plotted.length" class="arms-empty">日线还没有回到库里，图先空着</div>
          <div v-else ref="armsChartRef" class="arms-chart"></div>
        </section>
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
          :options="trendSelectOptions"
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
          <span><em>排列</em><b :class="trendTone(trendCursor, 'ma_order')">{{ trendCursor.ma_order || '—' }}</b></span>
        </div>
      </div>
      <div v-if="trendCursor" class="trend-topline">
        <span class="is-ma5"><em>MA5</em><b>{{ adviceNum(trendCursor.ma5) }}</b></span>
        <span class="is-ma10"><em>MA10</em><b>{{ adviceNum(trendCursor.ma10) }}</b></span>
        <span class="is-ma"><em>MA20</em><b>{{ adviceNum(trendCursor.ma20) }}</b></span>
        <span class="is-vwap"><em>AVWAP</em><b>{{ adviceNum(trendCursor.anchor_vwap) }}</b></span>
        <span><em>Amihud</em><b>{{ adviceNum(trendCursor.illiq, 3) }}</b></span>
        <span><em>RSI</em><b>{{ adviceNum(trendCursor.rsi, 1) }}</b></span>
        <span><em>止损K线</em><b>{{ trendCursor.stop_candle || '—' }}</b></span>
      </div>
      <p v-if="trendCursor?.drawdown_pct != null && trendCursor?.rebound_pct != null" class="trend-swing">
        近 22 日从 {{ trendCursor.swing_high_date }} 高点回撤 {{ Number(trendCursor.drawdown_pct).toFixed(1) }}%，从 {{ trendCursor.swing_low_date }} 低点反弹 {{ Number(trendCursor.rebound_pct).toFixed(1) }}%
      </p>
      <div ref="trendChartRef" class="trend-chart"></div>
      <p class="sentiment-note trend-table-note">按交易日展示行情与指标；历史资金流、组合平价、PE 和 ERP 尚未保存，显示“—”。仓位上限按当日波动率计算。</p>
      <a-table
        class="trend-table"
        :columns="trendColumns"
        :data-source="trendRows"
        :loading="trendLoading"
        row-key="date"
        size="small"
        :pagination="false"
        :scroll="trendTableScroll"
      >
        <template #headerCell="{ column }">
          <span class="col-head">
            <span>{{ column.title }}</span>
            <a-tooltip v-if="indicatorHints[column.key] || trendUnavailableHints[column.key]" overlay-class-name="indicator-tip">
              <template #title>
                <div v-if="trendUnavailableHints[column.key]" class="hint-pop">
                  <p>{{ trendUnavailableHints[column.key] }}</p>
                </div>
                <div v-else class="hint-pop">
                  <p>{{ indicatorHints[column.key].text }}</p>
                  <p class="is-up"><b>上涨趋势</b>{{ indicatorHints[column.key].up }}</p>
                  <p class="is-range"><b>震荡趋势</b>{{ indicatorHints[column.key].range }}</p>
                  <p class="is-down"><b>下跌趋势</b>{{ indicatorHints[column.key].down }}</p>
                </div>
              </template>
              <QuestionCircleOutlined class="col-hint" />
            </a-tooltip>
          </span>
        </template>
        <template #bodyCell="{ column, record }">
          <span v-if="column.key === 'date'">{{ record.date }}</span>
          <span v-else-if="column.key === 'signals'" class="signal-cell" :class="record.strategies?.some((signal) => signal.ready) ? 'is-fired' : 'is-none'">
            <template v-if="record.strategies?.some((signal) => signal.ready)">
              <b v-for="signal in record.strategies.filter((signal) => signal.ready)" :key="signal.id" class="signal-tag" :title="signal.detail">{{ signal.name }}</b>
            </template>
            <template v-else>未触发</template>
          </span>
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
              <a class="trade-step-link" @click.prevent="goEditTrade(t)" v-html="tradeStepTitle(t)"></a>
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
.trend-table-note {
  margin: 12px 0 8px;
}

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
  color: var(--muted);
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
  background: var(--chip) !important;
}

.cycle-inner-table {
  margin: 0;
  background: var(--chip);
}

.cycle-inner-table :deep(.ant-table) {
  background: transparent;
}

.cycle-inner-table :deep(.ant-table-thead > tr > th) {
  background: var(--chip);
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
  background: var(--chip);
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
  color: var(--muted);
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
  background: var(--panel);
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
  color: var(--muted);
  font-size: 12px;
}

.calculator-overview strong {
  color: var(--text);
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
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--panel);
}

.calculator-result strong {
  color: var(--text);
  font-size: 16px;
  font-variant-numeric: tabular-nums;
}

.calculator-note {
  margin: 0;
  color: var(--muted);
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





.advice-point,
.advice-card p {
  margin: 0;
  color: var(--text);
  font-size: 13px;
  line-height: 1.65;
}








.market-hero {
  position: relative;
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(0, 62%);
  gap: 34px;
  align-items: center;
  overflow: hidden;
  margin-bottom: 16px;
  padding: 34px;
  border: 1px solid var(--border);
  border-radius: 20px;
  background: linear-gradient(122deg, color-mix(in srgb, var(--green) 5%, var(--panel)), var(--panel) 50%, color-mix(in srgb, var(--blue) 7%, var(--panel)));
}

.market-hero::after {
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

.market-hero-copy,
.market-hero-side,
.market-metrics {
  position: relative;
  z-index: 1;
}

.market-kicker {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 15px;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
}

.market-kicker-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #14977d;
  box-shadow: 0 0 0 4px rgba(20, 151, 125, 0.1);
}

.market-hero-copy h2 {
  margin: 0;
  font-size: clamp(30px, 3.6vw, 43px);
  font-weight: 680;
  line-height: 1.24;
  letter-spacing: -0.05em;
  color: var(--green);
}

.market-hero-note {
  max-width: 600px;
  margin: 17px 0 0;
  font-size: 13px;
  line-height: 1.85;
  color: var(--muted);
}

.market-hero-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  margin-top: 21px;
}

.market-hero-meta span {
  padding: 5px 9px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--panel);
  font-size: 10px;
  color: var(--muted);
}

.market-hero-side {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}
.breadth-books {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.breadth-book {
  appearance: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  padding: 14px 14px 12px;
  text-align: left;
  font: inherit;
  color: inherit;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 5px 18px rgba(46, 70, 105, 0.035);
  cursor: pointer;
}
.breadth-book:hover {
  border-color: var(--blue);
}
.breadth-book:focus-visible {
  outline: 2px solid var(--green);
  outline-offset: 2px;
}
.breadth-book.is-on {
  border-color: var(--green);
  background: var(--panel);
  box-shadow: 0 8px 20px rgba(33, 131, 111, 0.08);
}
.breadth-book-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.breadth-book-name {
  font-size: 13px;
  font-weight: 650;
  letter-spacing: -0.02em;
  color: var(--text);
}
.breadth-book-pair {
  flex: none;
  padding: 2px 6px;
  border-radius: 6px;
  background: rgba(33, 131, 111, 0.1);
  color: var(--green);
  font-size: 10px;
  font-weight: 650;
  line-height: 1.45;
}
.breadth-book-count {
  margin-top: -4px;
  color: var(--muted);
  font-size: 11px;
}
.breadth-book-sample {
  color: var(--muted);
  font-size: 10px;
  line-height: 1.4;
  font-variant-numeric: tabular-nums;
}
.breadth-book-stats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 12px;
  margin-top: 2px;
}
.breadth-book-stats > span {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-width: 0;
}
.breadth-book-label {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.breadth-book-stats .market-metric-icon {
  width: 18px;
  height: 18px;
  border-radius: 5px;
  font-size: 11px;
}
.breadth-book-stats em {
  font-style: normal;
  font-size: 11px;
  font-weight: 500;
  color: var(--muted);
}
.breadth-book-stats strong {
  display: block;
  font-size: 16px;
  font-weight: 680;
  line-height: 1.2;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}
.breadth-book-stats strong.pnl-up {
  color: #089981;
}
.breadth-book-stats strong.pnl-down {
  color: #f23645;
}
.main-flow-tiers {
  display: none;
}
.market-tape article p small {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 400;
}
.market-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.market-metrics article {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  min-width: 0;
  min-height: 118px;
  padding: 17px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--panel);
  box-shadow: 0 5px 18px rgba(46, 70, 105, 0.035);
}

.market-metrics article.is-arms {
  cursor: pointer;
}

.market-metrics article.is-arms:hover {
  border-color: var(--blue);
}

.metric-hit {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  width: 100%;
}

.market-metrics article:has(.metric-hit) {
  display: block;
}

.arms-modal-title-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px 16px;
  width: 100%;
  padding-right: 28px;
}

.arms-scope-toggle {
  display: inline-flex;
  padding: 3px;
  border-radius: 999px;
  background: var(--chip);
  border: 1px solid #e2e8f0;
}

.arms-scope-toggle button {
  appearance: none;
  border: 0;
  margin: 0;
  padding: 5px 12px;
  border-radius: 999px;
  background: transparent;
  color: var(--muted);
  font-size: 12px;
  font-weight: 650;
  cursor: pointer;
  line-height: 1.2;
}

.arms-scope-toggle button.is-on {
  background: #fff;
  color: var(--text);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
}

.arms-scope-toggle button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.arms-modal-title {
  color: var(--text);
  font-weight: 700;
}

.arms-compare {
  display: grid;
  gap: 14px;
}

.arms-compare.is-pair {
  grid-template-columns: 1fr 1fr;
}

.arms-compare-head {
  margin: 0 0 8px;
  color: var(--muted);
  font-size: 13px;
  font-weight: 650;
}

.arms-compare-col.is-narrow .arms-compare-head {
  color: #0f766e;
}

.arms-board {
  display: flex;
  flex-direction: column;
  gap: 12px;
  color: var(--text);
}

.arms-focus-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid #dbe4f0;
  border-radius: 10px;
  background: var(--chip);
  color: var(--muted);
  font-size: 12px;
}

.arms-kicker,
.arms-note {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}

.arms-cards,
.arms-views {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.arms-cards article,
.arms-views article,
.arms-chart-card,
.arms-synergy {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel);
}

.arms-cards article,
.arms-views article,
.arms-chart-card {
  padding: 16px;
}

.arms-card-head,
.arms-chart-head,
.arms-card-main,
.arms-split,
.arms-gauge-scale,
.arms-legend {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.arms-card-head h3 small {
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
}

.arms-tag {
  color: var(--muted);
  font-family: ui-monospace, Consolas, monospace;
  font-size: 11px;
}

.arms-tag.is-reverse {
  color: #fbbf24;
  padding: 1px 6px;
  border: 1px solid rgba(245, 158, 11, 0.35);
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.1);
}

.arms-figure {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 10px;
}

.arms-figure strong {
  display: inline;
}

.arms-card-head h3,
.arms-chart-head h3,
.arms-views h3,
.arms-synergy h3 {
  margin: 0;
  color: var(--text);
  font-size: 14px;
  font-weight: 650;
}

.arms-card-head span,
.arms-card-main p,
.arms-views p,
.arms-synergy p,
.arms-intensity p,
.arms-chart-head p,
.arms-gauge-scale,
.arms-legend {
  color: var(--muted);
  font-size: 12px;
}

.arms-card-head em {
  margin-left: auto;
  padding: 3px 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  font-style: normal;
  font-size: 12px;
  font-weight: 650;
}

.arms-card-head em.is-up { color: #10b981; background: rgba(16, 185, 129, 0.12); border-color: rgba(16, 185, 129, 0.4); }
.arms-card-head em.is-down { color: #f87171; background: rgba(239, 68, 68, 0.12); border-color: rgba(239, 68, 68, 0.4); }
.arms-card-head em.is-hot { color: #fbbf24; background: rgba(245, 158, 11, 0.14); border-color: rgba(245, 158, 11, 0.4); }
.arms-card-head em.is-ice { color: #c084fc; background: rgba(168, 85, 247, 0.16); border-color: rgba(168, 85, 247, 0.45); }
.arms-card-head em.is-flat { color: var(--muted); background: var(--chip); border-color: #e2e8f0; }

.arms-synergy.is-up { background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.4); }
.arms-synergy.is-down { background: rgba(239, 68, 68, 0.08); border-color: rgba(239, 68, 68, 0.4); }
.arms-synergy.is-hot { background: rgba(245, 158, 11, 0.08); border-color: rgba(245, 158, 11, 0.4); }
.arms-synergy.is-ice { background: rgba(168, 85, 247, 0.1); border-color: rgba(168, 85, 247, 0.45); }
.arms-synergy.is-flat { background: var(--chip); border-color: #e2e8f0; }
.arms-synergy p strong {
  margin-left: 6px;
  font-size: 14px;
}

.arms-synergy.is-up p strong { color: #10b981; }
.arms-synergy.is-down p strong { color: #f87171; }
.arms-synergy.is-hot p strong { color: #fbbf24; }
.arms-synergy.is-ice p strong { color: #c084fc; }
.arms-synergy.is-flat p strong { color: var(--text); }

.arms-gauge-scale,
.arms-ratio-scale {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 10px;
  font-size: 11px;
  color: var(--muted);
}

.arms-ratio-scale {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #e2e8f0;
}

.arms-gauge-scale .is-panic {
  color: #c084fc;
  font-weight: 650;
}

.arms-card-main .is-side em {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  font-style: normal;
  font-weight: 650;
}

.arms-card-main .is-side em.is-up { color: #10b981; }
.arms-card-main .is-side em.is-down { color: #f87171; }
.arms-card-main .is-side em.is-flat { color: var(--muted); }

.arms-card-main {
  align-items: flex-end;
  margin: 14px 0;
}

.arms-card-main strong,
.arms-card-main b {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
}

.arms-card-main strong {
  display: block;
  color: var(--text);
  font-size: 36px;
  line-height: 1.1;
}

.arms-figure strong {
  display: inline;
}

.arms-card-main b {
  color: #38bdf8;
  font-size: 24px;
}

.arms-card-main .is-side {
  text-align: right;
}

.arms-card-main span,
.arms-split span {
  color: var(--text);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.arms-card-main span {
  display: inline-block;
  margin-top: 6px;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--chip);
}

.arms-split {
  margin-bottom: 8px;
}

.arms-split .is-up,
.arms-intensity .is-up,
.arms-card-main span.is-up {
  color: #10b981;
}

.arms-split .is-down,
.arms-intensity .is-down,
.arms-card-main span.is-down {
  color: #f87171;
}

.arms-bar,
.arms-gauge {
  position: relative;
  display: flex;
  gap: 2px;
  height: 12px;
  overflow: hidden;
  border-radius: 999px;
  background: #e8edf3;
}

.arms-bar i {
  display: block;
  height: 100%;
}

.arms-bar .is-up { background: #10b981; }
.arms-bar .is-flat { background: #64748b; }
.arms-bar .is-down { background: #ef4444; }

.arms-gauge {
  margin-top: 6px;
  overflow: visible;
  background: linear-gradient(90deg, #f59e0b 0%, #10b981 20%, #64748b 30%, #ef4444 58%, #a855f7 100%);
}

.arms-gauge i,
.arms-gauge b {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
}

.arms-gauge .is-one { left: 28.57%; background: rgba(255, 255, 255, 0.7); }
.arms-gauge .is-two { left: 64.28%; background: rgba(216, 180, 254, 0.85); }

.arms-gauge b {
  width: 12px;
  height: 20px;
  margin-top: -4px;
  border: 2px solid #020617;
  border-radius: 4px;
  background: var(--panel);
  transform: translateX(-50%);
}

.arms-synergy {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 16px;
}

.arms-synergy-icon {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: var(--chip);
  font-size: 18px;
}

.arms-synergy h3 {
  margin: 2px 0;
}

.arms-synergy span,
.arms-views span {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}

.arms-intensity {
  margin-left: auto;
  max-width: 220px;
  text-align: right;
}

.arms-intensity b {
  font-size: 12px;
  font-weight: 650;
}

.arms-views article {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.arms-chart-head {
  align-items: flex-start;
  margin-bottom: 8px;
}

.arms-chart-head p {
  margin: 4px 0 0;
}

.arms-legend {
  justify-content: flex-end;
  flex-wrap: wrap;
}

.arms-legend i {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-right: 4px;
  border-radius: 2px;
  vertical-align: -1px;
}

.arms-legend .is-index { background: #fbbf24; }
.arms-legend .is-ratio { background: #38bdf8; }
.arms-legend .is-panic { background: #a855f7; }
.arms-legend .is-narrow { background: #0f766e; }
.arms-legend .is-sse { background: #ef4444; }
.arms-legend .is-hs300 { background: #f59e0b; }
.arms-legend .is-djia { background: #ef4444; }
.arms-legend .is-spx { background: #f59e0b; }
.arms-legend .is-ndx { background: #3b82f6; }
.arms-legend .is-narrow-ma20 { background: #6366f1; }
.arms-legend .is-full-trin { background: #475569; }
.arms-legend .is-full-ma5 {
  background: transparent;
  border: 2px dotted #64748b;
  border-radius: 0;
  height: 0;
  width: 12px;
}

.arms-empty {
  padding: 48px 0;
  text-align: center;
  color: var(--muted);
}

.arms-chart {
  height: 430px;
}

.market-metric-icon {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 32px;
  height: 32px;
  border-radius: 9px;
  font-size: 15px;
}

.market-metric-icon.blue { color: #4b6de0; background: color-mix(in srgb, #4b6de0 12%, var(--panel)); }
.market-metric-icon.teal { color: #14977d; background: color-mix(in srgb, #14977d 12%, var(--panel)); }
.market-metric-icon.amber { color: #b78631; background: color-mix(in srgb, #b78631 12%, var(--panel)); }
.market-metric-icon.violet { color: #8a68c9; background: color-mix(in srgb, #8a68c9 12%, var(--panel)); }

.market-metrics p {
  margin: 1px 0 7px;
  font-size: 10px;
  color: var(--muted);
}

.market-metrics strong {
  display: block;
  font-size: 21px;
  line-height: 1.22;
  letter-spacing: -0.035em;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}

.market-metrics strong.pnl-up,
.market-tape strong.pnl-up,
.market-tape span.pnl-up {
  color: #089981;
}

.market-metrics strong.pnl-down,
.market-tape strong.pnl-down,
.market-tape span.pnl-down {
  color: #f23645;
}

.market-metrics article > div > span:not(.market-metric-icon),
.market-metrics .metric-hit > div > span {
  display: block;
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--muted);
}

.market-tape {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 8px;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--chip);
}

.market-tape-row {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(0, 1fr);
  gap: 12px;
}

.market-tape article {
  min-width: 0;
  padding: 14px 15px;
  border: 1px solid var(--border);
  border-radius: 11px;
  background: var(--panel);
}

.market-tape article.is-linked {
  cursor: pointer;
}

.market-tape article.is-linked:hover {
  border-color: var(--blue);
}

.market-tape p {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 650;
  color: var(--text);
}

.market-tape strong {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 20px;
  line-height: 1.2;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}

.market-tape strong > span {
  font-size: 13px;
  font-weight: 650;
}

.market-tape article > span {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

.market-tape article > span > span {
  margin-left: 6px;
  font-weight: 650;
}

.market-tape .sentiment-chart {
  height: 228px;
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
  .market-hero {
    grid-template-columns: 1fr;
    gap: 26px;
    padding: 28px;
  }

  .market-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .breadth-books {
    grid-template-columns: 1fr;
  }

  .breadth-book-stats {
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 8px 6px;
  }

  .breadth-book-stats strong {
    font-size: 14px;
  }

  .market-metrics .is-main-flow {
    grid-column: 1 / -1;
  }

  .is-main-flow .metric-hit {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    grid-template-areas:
      "icon title amount"
      "icon ratio amount";
    align-items: center;
    column-gap: 0px;
    row-gap: 2px;
  }

  .is-main-flow .metric-hit > div {
    display: contents;
  }

  .is-main-flow .metric-hit .market-metric-icon {
    grid-area: icon;
    align-self: start;
    width: 18px;
    height: 18px;
    border-radius: 5px;
    font-size: 11px;
  }

  .is-main-flow .metric-hit p {
    grid-area: title;
    margin: 0;
    font-size: 13px;
    font-weight: 650;
    color: var(--text);
  }

  .is-main-flow .metric-hit strong {
    grid-area: amount;
    justify-self: end;
    align-self: start;
    font-size: 18px;
  }

  .is-main-flow .metric-hit > div > span {
    grid-area: ratio;
    margin-top: 0;
  }

  .main-flow-tiers {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px 6px;
    margin-top: 12px;
  }

  .main-flow-tiers > span {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .main-flow-tiers .market-metric-icon {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    font-size: 10px;
  }

  .main-flow-tiers em {
    font-style: normal;
    font-size: 11px;
    font-weight: 500;
    color: var(--muted);
  }

  .market-metrics .main-flow-tiers strong {
    display: block;
    font-size: 13px;
    font-weight: 680;
    letter-spacing: -0.03em;
    font-variant-numeric: tabular-nums;
    line-height: 1.2;
  }
}

@media (max-width: 800px) {

  .market-hero {
    padding: 24px;
  }

  .market-tape {
    gap: 8px;
    padding: 12px;
  }

  .market-tape-row {
    grid-auto-flow: row;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .market-tape article {
    padding: 10px 8px;
  }

  .market-tape p,
  .market-tape article > span {
    font-size: 11px;
  }

  .market-tape strong {
    font-size: 15px;
  }
}

@media (max-width: 560px) {



  .market-hero {
    padding: 22px 18px;
    border-radius: 16px;
  }

  .market-hero-copy h2 {
    font-size: 28px;
  }

  .market-metrics article {
    min-height: 0;
    padding: 13px;
  }

  .market-metrics strong {
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

:global(.indicator-tip .ant-tooltip-inner) {
  width: 280px;
  padding: 10px 12px;
}

:global(.indicator-tip .hint-pop p) {
  margin: 0 0 6px;
  font-size: 12px;
  line-height: 1.5;
}

:global(.indicator-tip .hint-pop p:last-child) {
  margin-bottom: 0;
}

:global(.indicator-tip .hint-pop b) {
  display: inline-block;
  margin-right: 6px;
  font-weight: 650;
}

:global(.indicator-tip .hint-pop .is-up b) {
  color: #3dcca8;
}

:global(.indicator-tip .hint-pop .is-range b) {
  color: #e6b45a;
}

:global(.indicator-tip .hint-pop .is-down b) {
  color: #ff8a96;
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
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel);
  font-variant-numeric: tabular-nums;
}

.trend-quote-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.trend-quote-date {
  color: var(--muted);
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
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 6px;
}

.trend-quote-grid span {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  padding: 6px 8px;
  border-radius: 8px;
  background: var(--chip);
}

.trend-quote-grid em {
  color: var(--muted);
  font-size: 11px;
  font-style: normal;
  font-weight: 600;
  line-height: 1.2;
}

.trend-quote-grid b {
  color: var(--text);
  font-size: 13px;
  line-height: 1.2;
  white-space: nowrap;
}

.trend-quote .is-ma5 b,
.trend-topline .is-ma5 b {
  color: #8b93a3;
}

.trend-quote .is-ma10 b,
.trend-topline .is-ma10 b {
  color: #7c5cbf;
}

.trend-quote .is-ma b,
.trend-topline .is-ma b {
  color: #f07a1a;
}

.trend-quote .is-vwap b,
.trend-topline .is-vwap b {
  color: #089981;
}

.trend-topline {
  display: flex;
  justify-content: flex-start;
  flex-wrap: wrap;
  gap: 14px;
  margin: 0 10px;
  font-size: 10px;
}

.trend-topline em {
  margin-right: 4px;
  color: var(--muted);
  font-style: normal;
  font-weight: 650;
}

.trend-topline b {
  font-weight: 650;
}

.trend-swing {
  margin: 0 0 10px;
  color: var(--text);
  font-size: 13px;
  line-height: 1.5;
}

.signal-trigger {
  display: block;
  width: 100%;
  min-height: 32px;
  padding: 4px 2px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: center;
  cursor: pointer;
}

.signal-trigger:hover:not(:disabled) {
  background: var(--chip);
}

.signal-trigger:disabled {
  cursor: default;
}

.signal-cell { display: inline-flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 4px; font-size: 11px; }
.signal-cell.is-none, .signal-cell.is-wait { color: var(--muted); }
.signal-tag { padding: 2px 6px; border-radius: 5px; background: #e6f6f1; color: #12866f; font-size: 10px; font-weight: 650; white-space: nowrap; }
.signal-cell.is-fired { cursor: help; }

.trend-chart {
  width: 100%;
  height: 640px;
  margin-bottom: 8px;
}

.trend-table {
  max-width: 100%;
}

.trend-table :deep(.ant-table) {
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
}

.trend-table :deep(.ant-table-thead > tr > th) {
  background: var(--chip);
  color: var(--muted);
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
  color: var(--text);
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
  border: 1px solid var(--border);
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

  .trend-chart {
    height: 500px;
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








.toolbar-actions .advice-note {
  max-width: 420px;
}

@media (max-width: 800px) {
  .arms-compare.is-pair {
    grid-template-columns: 1fr;
  }

  .arms-cards,
  .arms-views,
  .arms-synergy,
  .arms-chart-head {
    display: flex;
    flex-direction: column;
    align-items: stretch;
  }

  .arms-card-head,
  .arms-chart-head,
  .arms-split,
  .arms-gauge-scale,
  .arms-legend,
  .arms-figure,
  .arms-card-main {
    flex-wrap: wrap;
    align-items: flex-start;
  }

  .arms-cards article,
  .arms-views article,
  .arms-chart-card {
    padding: 12px;
  }

  .arms-card-head h3,
  .arms-chart-head h3 {
    font-size: 13px;
    line-height: 1.45;
  }

  .arms-card-head em {
    margin-left: 0;
  }

  .arms-card-main .is-side {
    text-align: left;
  }

  .arms-card-main strong {
    font-size: 28px;
  }

  .arms-gauge-scale {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 2px 10px;
    font-size: 10px;
    line-height: 1.4;
  }

  .arms-gauge-scale span {
    white-space: nowrap;
  }

  .arms-intensity {
    margin-left: 0;
    max-width: none;
    text-align: left;
  }

  .arms-chart {
    height: 320px;
  }
}

:global(html[data-theme='dark'] .signal-tag) { background: #1b322c; color: #4fd1b5; }

:global(html[data-theme='dark'] .trend-brief-grid article) {
  background: #262b36;
  border-color: var(--border);
}

:global(html[data-theme='dark'] .trend-brief-grid h3) {
  color: #5dbea8;
}

:global(html[data-theme='dark'] .trend-levels .is-stop) {
  background: rgba(242, 54, 69, 0.12);
  border-color: rgba(242, 54, 69, 0.35);
}

:global(html[data-theme='dark'] .trend-levels .is-target) {
  background: rgba(8, 153, 129, 0.12);
  border-color: rgba(8, 153, 129, 0.35);
}

:global(html[data-theme='dark'] .trend-mark.is-up) {
  background: rgba(8, 153, 129, 0.16);
  color: #5dbea8;
}

:global(html[data-theme='dark'] .trend-mark.is-down) {
  background: rgba(242, 54, 69, 0.16);
  color: #ff8b95;
}

:global(html[data-theme='dark'] .trend-mark.is-warn) {
  background: rgba(230, 167, 0, 0.16);
  color: #f0c14d;
}

:global(html[data-theme='dark'] .trend-mark.is-info) {
  background: rgba(59, 111, 216, 0.16);
  color: #8eb4ff;
}





:global(html[data-theme='dark'] .trend-chart) {
  background: var(--panel);
}
</style>

<style>
.arms-modal .ant-modal {
  max-width: calc(100vw - 16px);
}

.arms-modal .ant-modal-content {
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
}

.arms-modal .ant-modal-header {
  background: transparent;
}

.arms-modal .ant-modal-close {
  color: var(--muted);
}

.arms-modal .ant-modal-body {
  max-height: calc(100vh - 140px);
  overflow: auto;
}

@media (max-width: 800px) {
  .arms-modal .ant-modal {
    top: 8px;
    margin: 0 auto;
    max-width: calc(100vw - 12px);
    padding-bottom: 8px;
  }

  .arms-modal .ant-modal-content {
    padding: 0;
    border-radius: 12px;
  }

  .arms-modal .ant-modal-header {
    padding: 12px 40px 8px 14px;
  }

  .arms-modal .ant-modal-title {
    font-size: 15px;
    line-height: 1.4;
    white-space: normal;
  }

  .arms-modal .ant-modal-body {
    padding: 8px 12px 14px;
    max-height: calc(100vh - 72px);
  }
}
</style>
