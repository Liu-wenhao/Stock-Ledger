<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { fetchMovers, fetchNav, fetchTotalMovers } from '@/api/portfolio'
import { formatMoney, formatPct, pnlClass, toNumber } from '@/utils/format'
import {
  axisLabelStyle,
  axisLineStyle,
  moneyAxisFormatter,
  noSplitLine,
  pctAxisFormatter,
  zeroMarkLine,
} from '@/utils/chart'
import SymbolCell from '@/components/SymbolCell.vue'

const mode = ref('value')
const range = ref('3M')
const includeClosed = ref(false)
const nav = ref({ points: [], ranges: [] })
const movers = ref({ winners: [], losers: [] })
const totalMovers = ref({ winners: [], losers: [] })
const chartRef = ref(null)
const DAILY_LIMIT = 5
let chart

function moverMetric(item) {
  return mode.value === 'percent' ? toNumber(item.change_percent) : toNumber(item.change)
}

function formatMover(item) {
  if (mode.value === 'percent') return formatPct(item.change_percent)
  return formatMoney(item.change).replace(' USD', '')
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

function rankMovers(list, direction, limit = null) {
  const arr = uniqueBySymbol(list).filter((i) => {
    const v = moverMetric(i)
    return direction === 'up' ? v > 0 : v < 0
  })
  arr.sort((a, b) =>
    direction === 'up' ? moverMetric(b) - moverMetric(a) : moverMetric(a) - moverMetric(b),
  )
  return limit == null ? arr : arr.slice(0, limit)
}

const dailyPool = computed(() =>
  uniqueBySymbol([...(movers.value.winners || []), ...(movers.value.losers || [])]),
)
const totalPool = computed(() =>
  uniqueBySymbol([...(totalMovers.value.winners || []), ...(totalMovers.value.losers || [])]),
)
const dailyWinners = computed(() => rankMovers(dailyPool.value, 'up', DAILY_LIMIT))
const dailyLosers = computed(() => rankMovers(dailyPool.value, 'down', DAILY_LIMIT))
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

async function loadTotalMovers() {
  totalMovers.value = await fetchTotalMovers({ include_closed: includeClosed.value })
}

async function load() {
  const [navRes, moversRes] = await Promise.all([
    fetchNav({ range: range.value, mode: mode.value }),
    fetchMovers(),
  ])
  nav.value = navRes
  movers.value = moversRes
  await loadTotalMovers()
  await nextTick()
  renderChart()
}

function renderChart() {
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value)

  const points = nav.value.points || []
  const isPct = mode.value === 'percent'
  const dates = points.map((p) => p.date)
  const main = points.map((p) => toNumber(isPct ? p.return_percent : p.total_value))
  const secondary = points.map((p) => toNumber(isPct ? p.cash_percent : p.cash))
  const lastMain = main.length ? main[main.length - 1] : null
  const lastSec = secondary.length ? secondary[secondary.length - 1] : null

  chart.setOption(
    {
      animationDuration: 400,
      grid: { left: 12, right: 72, top: 28, bottom: 28 },
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
          interval: Math.max(Math.floor(dates.length / 8), 0),
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
      series: [
        {
          name: isPct ? '收益率' : '投资组合金额',
          type: 'line',
          data: main,
          smooth: 0.25,
          showSymbol: false,
          z: 3,
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(41,98,255,0.22)' },
              { offset: 1, color: 'rgba(41,98,255,0.02)' },
            ]),
          },
          lineStyle: { color: '#2962ff', width: 2 },
          itemStyle: { color: '#2962ff' },
          markLine: zeroMarkLine,
          endLabel: lastMain == null
            ? undefined
            : {
                show: true,
                formatter: () => (isPct ? formatPct(lastMain) : moneyAxisFormatter(lastMain)),
                color: '#fff',
                backgroundColor: '#2962ff',
                padding: [3, 6],
                borderRadius: 3,
                fontSize: 11,
                fontWeight: 600,
                distance: 6,
              },
        },
        {
          name: isPct ? '现金收益率' : '现金',
          type: 'line',
          data: secondary,
          smooth: 0.25,
          showSymbol: false,
          z: 2,
          lineStyle: { color: '#26a69a', width: 2 },
          itemStyle: { color: '#26a69a' },
          endLabel: lastSec == null
            ? undefined
            : {
                show: true,
                formatter: () => (isPct ? formatPct(lastSec) : moneyAxisFormatter(lastSec)),
                color: '#fff',
                backgroundColor: '#26a69a',
                padding: [3, 6],
                borderRadius: 3,
                fontSize: 11,
                fontWeight: 600,
                distance: 6,
              },
        },
      ],
    },
    true,
  )
}

function onResize() {
  chart?.resize()
}

watch([mode, range], load)
watch(includeClosed, loadTotalMovers)
onMounted(() => {
  load()
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="overview">
    <section class="change-section">
      <h2 class="section-title">投资组合更改</h2>

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
          class="range-chip"
          :class="{ active: range === r.key }"
          @click="range = r.key"
        >
          <div class="r-label">{{ r.label }}</div>
          <div class="r-value" :class="pnlClass(r.return_percent)">
            {{ r.return_percent == null ? '—' : formatPct(r.return_percent) }}
          </div>
        </button>
      </div>
    </section>

    <div class="movers">
      <section class="mover-panel">
        <h3 class="mover-title">每日赢家</h3>
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
        <h3 class="mover-title">每日输家</h3>
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

    <div class="total-movers-header">
      <h2 class="section-title">汇总</h2>
      <button
        type="button"
        class="mode-btn"
        :class="{ active: includeClosed }"
        @click="includeClosed = !includeClosed"
      >
        显示已清仓
      </button>
    </div>

    <div class="movers total-movers">
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
  grid-template-columns: repeat(6, minmax(0, 1fr));
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

.movers {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-top: 28px;
  padding-top: 8px;
}

.total-movers-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 36px;
}

.total-movers-header .section-title {
  margin: 0;
}

.total-movers {
  margin-top: 12px;
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
    grid-template-columns: 1fr;
  }
}
</style>
