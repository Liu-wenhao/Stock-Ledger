<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { fetchMovers, fetchNav, fetchPeriodPerformance, refreshMarket } from '@/api/portfolio'
import { formatMoney, formatPct, pnlClass, toNumber } from '@/utils/format'
import { isUsMarketOpen } from '@/utils/marketHours'
import {
  axisLabelStyle,
  axisLineStyle,
  moneyAxisFormatter,
  noSplitLine,
  pctAxisFormatter,
  zeroMarkLine,
} from '@/utils/chart'
import SymbolCell from '@/components/SymbolCell.vue'
import { usePortfolioStore } from '@/stores/portfolio'

const emit = defineEmits(['changed'])
const store = usePortfolioStore()
const mode = ref('value')
const range = ref('1W')
const periodPerformance = ref(null)

const CHART_SERIES = [
  {
    valueKey: 'total_value',
    pctKey: 'return_percent',
    labelValue: '组合',
    labelPct: '组合',
    color: '#2962ff',
    z: 3,
    area: true,
    markLine: true,
    endLabel: true,
  },
  {
    valueKey: 'holdings_value',
    pctKey: 'holdings_percent',
    labelValue: '投资',
    labelPct: '投资',
    color: '#f59e0b',
    z: 2,
    area: false,
    markLine: false,
    endLabel: true,
  },
  {
    valueKey: 'cash',
    pctKey: 'cash_percent',
    labelValue: '现金',
    labelPct: '现金',
    color: '#26a69a',
    z: 1,
    area: false,
    markLine: false,
    endLabel: true,
  },
  {
    valueKey: 'period_pnl',
    labelValue: '盈亏金额',
    color: '#ef5350',
    z: 2,
    area: false,
    zeroLine: true,
    endLabel: true,
  },
]

function formatPnlByMode(amount, percent) {
  if (mode.value === 'percent') return formatPct(percent)
  return formatMoney(amount).replace(/ [A-Z]{3}$/, '')
}

const dayPnlText = computed(() => {
  const s = store.summary
  if (!s) return '—'
  return formatPnlByMode(s.day_change, s.day_change_percent)
})

const dayPnlClass = computed(() => pnlClass(store.summary?.day_change))

const holdingPnlText = computed(() => {
  const section = periodPerformance.value?.holding
  if (!section) return '—'
  return formatPnlByMode(section.pnl, section.pnl_percent)
})

const holdingPnlClass = computed(() => pnlClass(periodPerformance.value?.holding?.pnl))

const closedPnlText = computed(() => {
  const section = periodPerformance.value?.closed
  if (!section) return '—'
  return formatPnlByMode(section.pnl, section.pnl_percent)
})

const closedPnlClass = computed(() => pnlClass(periodPerformance.value?.closed?.pnl))

const cumulativePnlText = computed(() => {
  const section = periodPerformance.value?.cumulative
  if (!section) return '—'
  return formatPnlByMode(section.pnl, section.pnl_percent)
})

const cumulativePnlClass = computed(() => pnlClass(periodPerformance.value?.cumulative?.pnl))

const nav = ref({ points: [], ranges: [] })
const movers = ref({ winners: [], losers: [] })
const holdingMovers = computed(() => periodPerformance.value?.holding || { winners: [], losers: [] })
const closedMovers = computed(() => periodPerformance.value?.closed || { winners: [], losers: [] })
const totalMovers = computed(() => periodPerformance.value?.cumulative || { winners: [], losers: [] })
const chartRef = ref(null)
let chart
let quoteRefreshTimer
let quoteRefreshBusy = false

const QUOTE_REFRESH_INTERVAL_MS = 10_000

function moverMetric(item) {
  return mode.value === 'percent' ? toNumber(item.change_percent) : toNumber(item.change)
}

function formatMover(item) {
  if (mode.value === 'percent') return formatPct(item.change_percent)
  return formatMoney(item.change).replace(/ [A-Z]{3}$/, '')
}

function barWidth(item, peers) {
  const abs = Math.abs(moverMetric(item))
  const max = Math.max(...(peers || []).map((i) => Math.abs(moverMetric(i))), abs, 0.01)
  return `${Math.min((abs / max) * 100, 100)}%`
}

function uniqueBySymbol(list) {
  const map = new Map()
  for (const item of list || []) map.set(item.symbol, item)
  return [...map.values()]
}

function rankMovers(list, direction) {
  const arr = uniqueBySymbol(list).filter((i) => {
    const v = moverMetric(i)
    return direction === 'up' ? v > 0 : v < 0
  })
  arr.sort((a, b) =>
    direction === 'up' ? moverMetric(b) - moverMetric(a) : moverMetric(a) - moverMetric(b),
  )
  return arr
}

const dailyPool = computed(() =>
  uniqueBySymbol([...(movers.value.winners || []), ...(movers.value.losers || [])]),
)
const holdingPool = computed(() =>
  uniqueBySymbol([...(holdingMovers.value.winners || []), ...(holdingMovers.value.losers || [])]),
)
const closedPool = computed(() =>
  uniqueBySymbol([...(closedMovers.value.winners || []), ...(closedMovers.value.losers || [])]),
)
const totalPool = computed(() =>
  uniqueBySymbol([...(totalMovers.value.winners || []), ...(totalMovers.value.losers || [])]),
)
// 每日盈利 / 每日亏损：列出全部持仓（涨的进盈利、跌的进亏损），
// 不再只截前 5 名 —— 那样持仓一多就看不到大部分股票。
const dailyWinners = computed(() => rankMovers(dailyPool.value, 'up'))
const dailyLosers = computed(() => rankMovers(dailyPool.value, 'down'))
const holdingWinners = computed(() => rankMovers(holdingPool.value, 'up'))
const holdingLosers = computed(() => rankMovers(holdingPool.value, 'down'))
const closedWinners = computed(() => rankMovers(closedPool.value, 'up'))
const closedLosers = computed(() => rankMovers(closedPool.value, 'down'))
const totalWinners = computed(() => rankMovers(totalPool.value, 'up'))
const totalLosers = computed(() => rankMovers(totalPool.value, 'down'))

function formatDateLabel(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const month = d.getMonth() + 1
  const day = d.getDate()
  // Show month on first day-ish labels; otherwise day
  if (day === 1 || day === 15) return `${month}月`
  return `${day}日`
}

async function load() {
  const [navRes, moversRes, periodRes] = await Promise.all([
    fetchNav({ range: range.value, mode: mode.value }),
    fetchMovers(),
    fetchPeriodPerformance({ range: range.value }),
  ])
  nav.value = navRes
  movers.value = moversRes
  periodPerformance.value = periodRes
  await nextTick()
  renderChart()
}

async function autoRefreshQuotes() {
  if (quoteRefreshBusy || !isUsMarketOpen()) return
  quoteRefreshBusy = true
  try {
    await refreshMarket(true)
    await load()
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

function formatRangeChip(r) {
  if (mode.value === 'percent') {
    return r.return_percent == null ? '—' : formatPct(r.return_percent)
  }
  return r.pnl == null ? '—' : formatMoney(r.pnl).replace(/ [A-Z]{3}$/, '')
}

function rangeChipClass(r) {
  return mode.value === 'percent' ? pnlClass(r.return_percent) : pnlClass(r.pnl)
}

function enrichChartPoints(points) {
  const startPnl = points.length ? toNumber(points[0].total_pnl) : 0
  return points.map((p) => ({
    ...p,
    period_pnl: toNumber(p.total_pnl) - startPnl,
  }))
}

function activeChartSeries(isPct) {
  if (isPct) return CHART_SERIES.filter((cfg) => cfg.pctKey)
  return CHART_SERIES.filter((cfg) => cfg.valueKey)
}

function buildChartSeries(points, isPct, compact) {
  const enriched = enrichChartPoints(points)
  return activeChartSeries(isPct).map((cfg) => {
    const dataKey = isPct ? cfg.pctKey : cfg.valueKey
    const data = enriched.map((p) => toNumber(p[dataKey]))
    const lastValue = data.length ? data[data.length - 1] : null
    const name = isPct ? `${cfg.labelPct}收益率` : cfg.labelValue
    return {
      name,
      type: 'line',
      data,
      smooth: 0.25,
      showSymbol: false,
      z: cfg.z,
      areaStyle: cfg.area
        ? {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(41,98,255,0.22)' },
              { offset: 1, color: 'rgba(41,98,255,0.02)' },
            ]),
          }
        : undefined,
      lineStyle: { color: cfg.color, width: 2 },
      itemStyle: { color: cfg.color },
      markLine: (isPct && cfg.markLine) || (!isPct && cfg.zeroLine) ? zeroMarkLine : undefined,
      endLabel:
        lastValue == null || compact || !cfg.endLabel
          ? undefined
          : {
              show: true,
              formatter: () => (isPct ? formatPct(lastValue) : moneyAxisFormatter(lastValue)),
              color: '#fff',
              backgroundColor: cfg.color,
              padding: [3, 6],
              borderRadius: 3,
              fontSize: 11,
              fontWeight: 600,
              distance: 6,
            },
    }
  })
}

function renderChart() {
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value)

  const compact = window.innerWidth <= 560
  const points = nav.value.points || []
  const isPct = mode.value === 'percent'
  const dates = points.map((p) => p.date)

  chart.setOption(
    {
      animationDuration: 400,
      grid: { left: compact ? 4 : 12, right: compact ? 38 : 72, top: 28, bottom: compact ? 22 : 28 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#fff',
        borderColor: '#e0e3eb',
        textStyle: { color: '#131722', fontSize: 12 },
        valueFormatter: (v) => (isPct ? formatPct(v) : moneyAxisFormatter(v)),
      },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLine: axisLineStyle,
        axisTick: { show: false },
        axisLabel: {
          ...axisLabelStyle,
          formatter: formatDateLabel,
          interval: Math.max(Math.floor(dates.length / (compact ? 4 : 8)), 0),
        },
      },
      yAxis: {
        type: 'value',
        scale: true,
        position: 'right',
        splitLine: noSplitLine,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          ...axisLabelStyle,
          formatter: (v) => (isPct ? pctAxisFormatter(v) : moneyAxisFormatter(v)),
        },
      },
      series: buildChartSeries(points, isPct, compact),
    },
    true,
  )
}

function onResize() {
  chart?.resize()
  renderChart()
}

watch([mode, range], load)
onMounted(() => {
  load()
  startQuoteRefreshTimer()
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  stopQuoteRefreshTimer()
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="overview">
    <section class="change-section">
      <div class="mode-toggle">
        <button
          type="button"
          class="mode-btn"
          :class="{ active: mode === 'value' }"
          @click="mode = 'value'"
        >
          金额
        </button>
        <button
          type="button"
          class="mode-btn"
          :class="{ active: mode === 'percent' }"
          @click="mode = 'percent'"
        >
          收益率
        </button>
      </div>

      <div ref="chartRef" class="nav-chart" />

      <div class="range-chips">
        <button
          v-for="r in nav.ranges || []"
          :key="r.key"
          type="button"
          size="small"
          class="range-chip"
          :class="{ active: range === r.key }"
          @click="range = r.key"
        >
          <div class="r-label">{{ r.label }}</div>
          <div class="r-value" :class="rangeChipClass(r)">
            {{ formatRangeChip(r) }}
          </div>
        </button>
      </div>
    </section>

    <section class="pnl-section">
      <h2 class="section-title">
        今日盈亏<span class="section-pnl" :class="dayPnlClass">（{{ dayPnlText }}）</span>
      </h2>
    </section>

    <div class="movers">
      <section class="mover-panel">
        <h3 class="mover-title">每日盈利</h3>
        <div v-for="item in dailyWinners" :key="item.symbol" class="mover-row">
          <SymbolCell :symbol="item.symbol" :name="item.name" :logo-url="item.logo_url" />
          <div class="mover-bar">
            <span class="bar-fill up" :style="{ width: barWidth(item, dailyWinners) }" />
          </div>
          <div class="mover-pct pnl-up">{{ formatMover(item) }}</div>
        </div>
        <a-empty v-if="!dailyWinners.length" description="暂无数据" />
      </section>

      <section class="mover-panel">
        <h3 class="mover-title">每日亏损</h3>
        <div v-for="item in dailyLosers" :key="item.symbol + '-l'" class="mover-row">
          <SymbolCell :symbol="item.symbol" :name="item.name" :logo-url="item.logo_url" />
          <div class="mover-bar">
            <span class="bar-fill down" :style="{ width: barWidth(item, dailyLosers) }" />
          </div>
          <div class="mover-pct pnl-down">{{ formatMover(item) }}</div>
        </div>
        <a-empty v-if="!dailyLosers.length" description="暂无数据" />
      </section>
    </div>

    <section class="pnl-section">
      <h2 class="section-title">
        持仓盈亏<span class="section-pnl" :class="holdingPnlClass">（{{ holdingPnlText }}）</span>
      </h2>
    </section>

    <div class="movers">
      <section class="mover-panel">
        <h3 class="mover-title">总赢家</h3>
        <div v-for="item in holdingWinners" :key="'hw-' + item.symbol" class="mover-row">
          <SymbolCell :symbol="item.symbol" :name="item.name" :logo-url="item.logo_url" />
          <div class="mover-bar">
            <span class="bar-fill up" :style="{ width: barWidth(item, holdingWinners) }" />
          </div>
          <div class="mover-pct pnl-up">{{ formatMover(item) }}</div>
        </div>
        <a-empty v-if="!holdingWinners.length" description="暂无数据" />
      </section>

      <section class="mover-panel">
        <h3 class="mover-title">总输家</h3>
        <div v-for="item in holdingLosers" :key="'hl-' + item.symbol" class="mover-row">
          <SymbolCell :symbol="item.symbol" :name="item.name" :logo-url="item.logo_url" />
          <div class="mover-bar">
            <span class="bar-fill down" :style="{ width: barWidth(item, holdingLosers) }" />
          </div>
          <div class="mover-pct pnl-down">{{ formatMover(item) }}</div>
        </div>
        <a-empty v-if="!holdingLosers.length" description="暂无数据" />
      </section>
    </div>

    <section class="pnl-section">
      <h2 class="section-title">
        累计盈亏<span class="section-pnl" :class="cumulativePnlClass">（{{ cumulativePnlText }}）</span>
      </h2>
    </section>

    <div class="movers">
      <section class="mover-panel">
        <h3 class="mover-title">总赢家</h3>
        <div v-for="item in totalWinners" :key="'tw-' + item.symbol" class="mover-row">
          <SymbolCell :symbol="item.symbol" :name="item.name" :logo-url="item.logo_url" />
          <div class="mover-bar">
            <span class="bar-fill up" :style="{ width: barWidth(item, totalWinners) }" />
          </div>
          <div class="mover-pct pnl-up">{{ formatMover(item) }}</div>
        </div>
        <a-empty v-if="!totalWinners.length" description="暂无数据" />
      </section>

      <section class="mover-panel">
        <h3 class="mover-title">总输家</h3>
        <div v-for="item in totalLosers" :key="'tl-' + item.symbol" class="mover-row">
          <SymbolCell :symbol="item.symbol" :name="item.name" :logo-url="item.logo_url" />
          <div class="mover-bar">
            <span class="bar-fill down" :style="{ width: barWidth(item, totalLosers) }" />
          </div>
          <div class="mover-pct pnl-down">{{ formatMover(item) }}</div>
        </div>
        <a-empty v-if="!totalLosers.length" description="暂无数据" />
      </section>
    </div>

    <section class="pnl-section">
      <h2 class="section-title">
        清仓盈亏<span class="section-pnl" :class="closedPnlClass">（{{ closedPnlText }}）</span>
      </h2>
    </section>

    <div class="movers">
      <section class="mover-panel">
        <h3 class="mover-title">总赢家</h3>
        <div v-for="item in closedWinners" :key="'cw-' + item.symbol" class="mover-row">
          <SymbolCell :symbol="item.symbol" :name="item.name" :logo-url="item.logo_url" />
          <div class="mover-bar">
            <span class="bar-fill up" :style="{ width: barWidth(item, closedWinners) }" />
          </div>
          <div class="mover-pct pnl-up">{{ formatMover(item) }}</div>
        </div>
        <a-empty v-if="!closedWinners.length" description="暂无数据" />
      </section>

      <section class="mover-panel">
        <h3 class="mover-title">总输家</h3>
        <div v-for="item in closedLosers" :key="'cl-' + item.symbol" class="mover-row">
          <SymbolCell :symbol="item.symbol" :name="item.name" :logo-url="item.logo_url" />
          <div class="mover-bar">
            <span class="bar-fill down" :style="{ width: barWidth(item, closedLosers) }" />
          </div>
          <div class="mover-pct pnl-down">{{ formatMover(item) }}</div>
        </div>
        <a-empty v-if="!closedLosers.length" description="暂无数据" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.overview {
  padding-top: 4px;
}

.section-title {
  margin: 0 0 12px;
  font-size: 18px;
  font-weight: 700;
  color: #131722;
}

.mode-toggle {
  display: inline-flex;
  gap: 8px;
  margin-bottom: 8px;
}

.mode-btn {
  min-width: 80px;
  height: 28px;
  padding: 0 14px;
  border-radius: 14px;
  border: 1px solid #d1d4dc;
  background: #fff;
  color: #131722;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: 0.15s ease;
}

.mode-btn.active {
  background: #131722;
  border-color: #131722;
  color: #fff;
}

.nav-chart {
  width: 100%;
  height: 340px;
}

.range-chips {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 8px;
  margin-top: 8px;
}

.range-chip {
  border: none;
  background: transparent;
  border-radius: 8px;
  padding: 12px 8px;
  text-align: center;
  cursor: pointer;
  transition: background 0.15s ease;
}

.range-chip:hover {
  background: #f7f8fa;
}

.range-chip.active {
  background: #f0f3fa;
}

.r-label {
  font-size: 12px;
  color: #787b86;
  line-height: 1.3;
}

.r-value {
  margin-top: 6px;
  font-size: 13px;
  font-weight: 700;
}

.pnl-section {
  margin-top: 28px;
  padding-top: 8px;
}

.section-pnl {
  margin-left: 4px;
  font-weight: 700;
}

.movers {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-top: 20px;
  padding-top: 8px;
}

.mover-panel {
  min-width: 0;
}

.mover-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 700;
}

.mover-row {
  display: grid;
  grid-template-columns: 160px 1fr 60px;
  align-items: center;
  column-gap: 6px;
  row-gap: 10px;
  padding: 10px 0;
}

.mover-bar {
  height: 10px;
  border-radius: 5px;
  background: #eef1f6;
  overflow: hidden;
}

.bar-fill {
  display: block;
  height: 100%;
  border-radius: 5px;
}

.bar-fill.up {
  background: #089981;
}

.bar-fill.down {
  background: #f23645;
}

.mover-pct {
  width: 60px;
  text-align: right;
  font-weight: 700;
  font-size: 12px;
  white-space: nowrap;
}

@media (max-width: 800px) {
  .movers,
  .range-chips {
    grid-template-columns: 1fr 1fr;
  }

  .range-chips {
    gap: 6px;
  }

  .range-chip {
    min-height: 52px;
    padding: 9px 6px;
  }

  .mover-row {
    grid-template-columns: minmax(108px, 1fr) 64px;
    gap: 8px;
  }

  .mover-row .mover-bar {
    grid-column: 1 / -1;
    grid-row: 2;
    height: 7px;
  }

  .mover-row .mover-pct {
    width: auto;
    font-size: 12px;
  }
}

@media (max-width: 560px) {
  .nav-chart {
    height: 260px;
  }

  .mode-toggle {
    display: flex;
    gap: 6px;
    margin-bottom: 4px;
  }

  .mode-btn {
    height: 36px;
    flex: 1;
    border-radius: 10px;
  }

  .range-chips {
    grid-template-columns: repeat(4, minmax(0, 1fr));
    margin-top: 4px;
  }

  .range-chip {
    min-height: 58px;
    border: 1px solid #e0e3eb;
  }

  .range-chip.active {
    border-color: #c7d2fe;
  }

  .pnl-section {
    margin-top: 22px;
    padding-top: 4px;
  }

  .section-title {
    font-size: 16px;
    line-height: 1.4;
  }

  .movers {
    grid-template-columns: 1fr;
    gap: 18px;
    margin-top: 14px;
  }

  .mover-title {
    margin-bottom: 4px;
  }

  .mover-row {
    padding: 8px 0;
  }
}
</style>
