<script setup>
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { fetchMonthlyReturns, fetchProfitSeries } from '@/api/portfolio'
import { pnlClass, toNumber } from '@/utils/format'
import {
  axisLabelStyle,
  axisLineStyle,
  moneyAxisFormatter,
  noSplitLine,
  pctAxisFormatter,
  zeroMarkLine,
} from '@/utils/chart'

const range = ref('3M')
const monthly = ref({ items: [], benchmark: 'QQQ' })
const profit = ref({ points: [], ranges: [] })
const barRef = ref(null)
const lineRef = ref(null)
let barChart
let lineChart

function formatDateLabel(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const month = d.getMonth() + 1
  const day = d.getDate()
  if (day === 1) return `${month}月`
  return `${day}日`
}

function formatChipMoney(v) {
  if (v == null || v === '') return null
  const n = toNumber(v)
  const abs = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  const sign = n < 0 ? '-' : ''
  return { text: `${sign}${abs}`, currency: 'USD' }
}

async function load() {
  monthly.value = await fetchMonthlyReturns({ range: range.value })
  profit.value = await fetchProfitSeries({ range: range.value })
  await nextTick()
  renderBar()
  renderLine()
}

function renderBar() {
  if (!barRef.value) return
  if (!barChart) barChart = echarts.init(barRef.value)
  const compact = window.innerWidth <= 560
  const items = monthly.value.items || []
  const bench = monthly.value.benchmark || 'QQQ'
  barChart.setOption(
    {
      animationDuration: 300,
      legend: {
        bottom: 0,
        left: 'center',
        itemWidth: 10,
        itemHeight: 10,
        textStyle: { color: '#131722', fontSize: 12 },
        data: [`按投资组合`, `${bench}`],
      },
      grid: { left: compact ? 2 : 8, right: compact ? 40 : 56, top: 16, bottom: 40 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v) => pctAxisFormatter(v),
      },
      xAxis: {
        type: 'category',
        data: items.map((i) => i.month),
        axisTick: { show: false },
        axisLine: axisLineStyle,
        axisLabel: axisLabelStyle,
      },
      yAxis: {
        type: 'value',
        position: 'right',
        splitLine: noSplitLine,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          ...axisLabelStyle,
          formatter: pctAxisFormatter,
        },
      },
      series: [
        {
          name: '按投资组合',
          type: 'bar',
          barMaxWidth: 28,
          data: items.map((i) => Number(toNumber(i.portfolio_percent).toFixed(2))),
          itemStyle: { color: '#1e3a8a' },
          markLine: zeroMarkLine,
        },
        {
          name: `${bench}`,
          type: 'bar',
          barMaxWidth: 28,
          data: items.map((i) => Number(toNumber(i.benchmark_percent).toFixed(2))),
          itemStyle: { color: '#67e8f9' },
        },
      ],
    },
    true,
  )
}

function renderLine() {
  if (!lineRef.value) return
  if (!lineChart) lineChart = echarts.init(lineRef.value)
  const compact = window.innerWidth <= 560
  const points = profit.value.points || []
  const total = points.map((p) => toNumber(p.total_pnl))
  const realized = points.map((p) => toNumber(p.realized_pnl))
  const lastTotal = total.length ? total[total.length - 1] : null
  const lastRealized = realized.length ? realized[realized.length - 1] : null

  lineChart.setOption(
    {
      animationDuration: 300,
      legend: {
        top: 0,
        left: 0,
        itemWidth: 10,
        itemHeight: 10,
        textStyle: { color: '#131722', fontSize: 12 },
        data: ['累计盈亏', '清仓盈亏'],
      },
      grid: { left: compact ? 2 : 8, right: compact ? 38 : 72, top: 36, bottom: compact ? 22 : 28 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v) => moneyAxisFormatter(v),
      },
      xAxis: {
        type: 'category',
        data: points.map((p) => p.date),
        boundaryGap: false,
        axisTick: { show: false },
        axisLine: axisLineStyle,
        axisLabel: {
          ...axisLabelStyle,
          formatter: formatDateLabel,
          interval: Math.max(Math.floor(points.length / (compact ? 4 : 8)), 0),
        },
      },
      yAxis: {
        type: 'value',
        position: 'right',
        scale: true,
        splitLine: noSplitLine,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          ...axisLabelStyle,
          formatter: moneyAxisFormatter,
        },
      },
      series: [
        {
          name: '累计盈亏',
          type: 'line',
          smooth: 0.25,
          showSymbol: false,
          data: total,
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(41,98,255,0.16)' },
              { offset: 1, color: 'rgba(41,98,255,0.02)' },
            ]),
          },
          lineStyle: { color: '#2962ff', width: 2 },
          itemStyle: { color: '#2962ff' },
          markLine: zeroMarkLine,
          endLabel:
            lastTotal == null || compact
              ? undefined
              : {
                  show: true,
                  formatter: () => moneyAxisFormatter(lastTotal),
                  color: '#fff',
                  backgroundColor: '#2962ff',
                  padding: [3, 6],
                  borderRadius: 3,
                  fontSize: 11,
                  fontWeight: 600,
                },
        },
        {
          name: '清仓盈亏',
          type: 'line',
          smooth: 0.25,
          showSymbol: false,
          data: realized,
          lineStyle: { color: '#22d3ee', width: 2 },
          itemStyle: { color: '#22d3ee' },
          endLabel:
            lastRealized == null || compact
              ? undefined
              : {
                  show: true,
                  formatter: () => moneyAxisFormatter(lastRealized),
                  color: '#fff',
                  backgroundColor: '#22d3ee',
                  padding: [3, 6],
                  borderRadius: 3,
                  fontSize: 11,
                  fontWeight: 600,
                },
        },
      ],
    },
    true,
  )
}

function onResize() {
  barChart?.resize()
  lineChart?.resize()
  renderBar()
  renderLine()
}

watch(range, load)
onMounted(() => {
  load()
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  barChart?.dispose()
  lineChart?.dispose()
  barChart = null
  lineChart = null
})
</script>

<template>
  <div class="analysis">
    <h2 class="section-title">投资组合盈利能力</h2>
    <div ref="barRef" class="chart-box bar-chart" />

    <h2 class="section-title profit-title">获利</h2>
    <div ref="lineRef" class="chart-box line-chart" />

    <div class="profit-ranges">
      <button
        v-for="r in profit.ranges || []"
        :key="r.key"
        type="button"
        class="profit-chip"
        :class="{ active: range === r.key }"
        @click="range = r.key"
      >
        <div class="chip-label">{{ r.label }}</div>
        <div v-if="formatChipMoney(r.pnl)" class="chip-value" :class="pnlClass(r.pnl)">
          <span class="chip-num">{{ formatChipMoney(r.pnl).text }}</span>
          <span class="chip-ccy">{{ formatChipMoney(r.pnl).currency }}</span>
        </div>
        <div v-else class="chip-value muted">—</div>
      </button>
    </div>
  </div>
</template>

<style scoped>
.analysis {
  padding-top: 4px;
}

.section-title {
  margin: 0 0 8px;
  font-size: 18px;
  font-weight: 700;
  color: #131722;
  line-height: 1.3;
}

.profit-title {
  margin-top: 28px;
}

.chart-box {
  width: 100%;
}

.bar-chart {
  height: 300px;
}

.line-chart {
  height: 320px;
}

.profit-ranges {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.profit-chip {
  border: none;
  background: transparent;
  border-radius: 8px;
  padding: 12px 8px;
  text-align: center;
  cursor: pointer;
  transition: background 0.15s ease;
}

.profit-chip:hover {
  background: #f7f8fa;
}

.profit-chip.active {
  background: #f0f3fa;
}

.chip-label {
  font-size: 12px;
  color: #787b86;
  line-height: 1.35;
}

.chip-value {
  margin-top: 6px;
  font-weight: 700;
  font-size: 13px;
  line-height: 1.3;
}

.chip-num {
  font-size: 13px;
}

.chip-ccy {
  margin-left: 3px;
  font-size: 11px;
  font-weight: 600;
}

.muted {
  color: #787b86;
}

@media (max-width: 800px) {
  .profit-ranges {
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 560px) {
  .analysis {
    padding-top: 0;
  }

  .section-title {
    font-size: 16px;
  }

  .profit-title {
    margin-top: 22px;
  }

  .bar-chart {
    height: 250px;
  }

  .line-chart {
    height: 270px;
  }

  .profit-ranges {
    gap: 6px;
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .profit-chip {
    padding: 9px 6px;
    border: 1px solid #e0e3eb;
  }

  .profit-chip.active {
    border-color: #c7d2fe;
  }
}
</style>
