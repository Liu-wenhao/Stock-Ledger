<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { fetchBenchmarks, fetchMonthlyReturns, fetchProfitSeries } from '@/api/portfolio'
import { useTheme } from '@/composables/useTheme'
import { usePortfolioStore } from '@/stores/portfolio'
import { pnlClass, toNumber } from '@/utils/format'
import {
  axisLabelStyle,
  axisLineStyle,
  moneyAxisFormatter,
  noSplitLine,
  pctAxisFormatter,
  zeroMarkLine,
} from '@/utils/chart'

const store = usePortfolioStore()
const { isDark } = useTheme()
const currency = computed(() => ({ us: 'USD', cn: 'CNY', hk: 'HKD' }[store.market]))
const range = ref('3M')
const monthly = ref({ items: [], benchmark: 'QQQ' })
const profit = ref({ points: [], ranges: [] })
const barRef = ref(null)
const lineRef = ref(null)
const benchmarks = ref(null)
const dataLoading = ref(false)
const benchmarkLoading = ref(false)
const analysisError = ref('')
const benchmarkError = ref('')
const fxDirection = ref('cny_usd')
const defaultRanges = [
  { key: '1W', label: '1周' }, { key: '1M', label: '1个月' },
  { key: '3M', label: '3个月' }, { key: '6M', label: '6个月' },
  { key: 'YTD', label: '今年' }, { key: '1Y', label: '1年' },
  { key: 'ALL', label: '全部' },
]
const rangeOptions = computed(() => profit.value.ranges?.length ? profit.value.ranges : defaultRanges)
const benchmarkColors = {
  portfolio: '#2962ff', qqq: '#67e8f9', sp500: '#14977d', treasury: '#94a3b8',
  gold: '#d4a11e', bitcoin: '#f97316', fx: '#a77ce6',
}
const benchmarkDefinitions = [
  { key: 'sp500', label: '标普500' },
  { key: 'treasury', label: '3个月美债（无风险估算）' },
  { key: 'gold', label: '黄金（GLD ETF代理）' },
  { key: 'bitcoin', label: '比特币' },
  { key: 'fx', label: '美元兑人民币' },
]
let loadToken = 0
let benchmarkToken = 0
let barChart
let lineChart

function chartPalette() {
  return isDark.value
    ? { ink: '#d1d4dc', muted: '#868b96', border: '#363a45', panel: '#1e222d' }
    : { ink: '#131722', muted: '#787b86', border: '#e0e3eb', panel: '#fff' }
}

function returnNumber(value) {
  if (value == null || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function displayReturn(value) {
  const number = returnNumber(value)
  return number == null ? '—' : `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

function inverseReturn(value) {
  const number = returnNumber(value)
  return number == null || number <= -100 ? null : (1 / (1 + number / 100) - 1) * 100
}

const comparisonSeries = computed(() => benchmarkDefinitions.map((definition) => {
  const item = benchmarks.value?.series?.find((series) => series.key === definition.key) || definition
  if (item.key !== 'fx' || fxDirection.value !== 'cny_usd') return item
  return {
    ...item,
    label: '人民币兑美元',
    return_percent: inverseReturn(item.return_percent),
    points: (item.points || []).map((point) => ({ ...point, return_percent: inverseReturn(point.return_percent) })),
    note: 'CNY/USD：每元人民币可兑换的美元；由 USD/CNY 取倒数，正收益代表人民币升值。',
  }
}))

function monthlyReturn(points, start, end) {
  const startReturn = returnNumber(points.get(start))
  const endReturn = returnNumber(points.get(end))
  if (startReturn == null || endReturn == null || startReturn <= -100) return null
  return (1 + endReturn / 100) / (1 + startReturn / 100) * 100 - 100
}

const monthlyBenchmarks = computed(() => comparisonSeries.value.map((item) => {
  const points = new Map((item.points || []).map((point) => [point.date, point.return_percent]))
  return {
    ...item,
    values: (monthly.value.items || []).map((month) => monthlyReturn(points, month.start_date, month.end_date)),
  }
}))

function completedThrough() {
  const through = new Date()
  through.setUTCDate(through.getUTCDate() - 1)
  return through.toISOString().slice(0, 10)
}

function benchmarkParams() {
  const items = monthly.value.items || []
  const start = items[0]?.start_date
  const end = items[items.length - 1]?.end_date
  return start && end ? { start, end } : null
}

async function loadBenchmarks() {
  if (dataLoading.value) return
  const params = benchmarkParams()
  if (!params) return
  const token = ++benchmarkToken
  benchmarkLoading.value = true
  benchmarkError.value = ''
  try {
    const result = await fetchBenchmarks(params)
    if (token !== benchmarkToken) return
    benchmarks.value = result
    await nextTick()
    renderBar()
  } catch (exception) {
    if (token === benchmarkToken) {
      benchmarks.value = null
      renderBar()
      benchmarkError.value = exception?.response?.data?.detail || '基准数据加载失败，可重试；不会按零收益显示。'
    }
  } finally {
    if (token === benchmarkToken) benchmarkLoading.value = false
  }
}

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
  return { text: `${sign}${abs}`, currency: currency.value }
}

async function load() {
  const token = ++loadToken
  benchmarkToken += 1
  dataLoading.value = true
  benchmarkLoading.value = false
  analysisError.value = ''
  benchmarkError.value = ''
  benchmarks.value = null
  monthly.value = { ...monthly.value, items: [] }
  profit.value = { ...profit.value, points: [] }
  const results = await Promise.allSettled([
    fetchMonthlyReturns({ range: range.value, end: completedThrough() }),
    fetchProfitSeries({ range: range.value }),
  ])
  if (token !== loadToken) return
  if (results[0].status === 'fulfilled') monthly.value = results[0].value
  if (results[1].status === 'fulfilled') profit.value = results[1].value
  if (results.some((result) => result.status === 'rejected')) analysisError.value = '部分账户分析数据加载失败，请刷新重试。'
  dataLoading.value = false
  await nextTick()
  if (token !== loadToken) return
  renderBar()
  renderLine()
  loadBenchmarks()
}

function renderBar() {
  if (!barRef.value) return
  if (!barChart) barChart = echarts.init(barRef.value)
  const tone = chartPalette()
  const compact = window.innerWidth <= 560
  const items = monthly.value.items || []
  const bench = monthly.value.benchmark || 'QQQ'
  const chartSeries = [
    {
      name: `投资组合（${currency.value}）`,
      values: items.map((item) => returnNumber(item.portfolio_percent)),
      color: benchmarkColors.portfolio,
    },
    {
      name: bench,
      values: items.map((item) => returnNumber(item.benchmark_percent)),
      color: benchmarkColors.qqq,
    },
    ...monthlyBenchmarks.value.map((item) => ({
      name: item.label, values: item.values, color: benchmarkColors[item.key],
    })),
  ]
  const visibleMonths = compact ? 4 : 12
  const zoom = items.length > visibleMonths
  const zoomStart = Math.max(0, 100 - visibleMonths / items.length * 100)
  barChart.setOption(
    {
      animationDuration: 300,
      legend: {
        type: 'scroll',
        bottom: 0,
        left: 0,
        right: 0,
        itemWidth: 10,
        itemHeight: 10,
        pageTextStyle: { color: tone.muted },
        pageIconColor: tone.ink,
        textStyle: { color: tone.ink, fontSize: 12 },
      },
      grid: { left: compact ? 2 : 8, right: compact ? 44 : 56, top: 16, bottom: zoom ? 78 : 42 },
      tooltip: {
        trigger: 'axis',
        confine: true,
        axisPointer: { type: 'shadow' },
        backgroundColor: tone.panel,
        borderColor: tone.border,
        textStyle: { color: tone.ink },
        formatter: (params) => {
          const index = params[0]?.dataIndex ?? 0
          const month = items[index]
          if (!month) return ''
          const rows = chartSeries.map((item) => {
            const marker = params.find((entry) => entry.seriesName === item.name)?.marker || ''
            return `${marker}${item.name} ${displayReturn(item.values[index])}`
          })
          return `${month.month_key || month.month}<br/>${month.start_date} — ${month.end_date}<br/>${rows.join('<br/>')}`
        },
      },
      xAxis: {
        type: 'category',
        data: items.map((item) => item.month_key || item.month),
        axisTick: { show: false },
        axisLine: { ...axisLineStyle, lineStyle: { color: tone.border } },
        axisLabel: { ...axisLabelStyle, color: tone.muted, hideOverlap: true, formatter: (value) => compact ? value.slice(2) : value },
      },
      yAxis: {
        type: 'value',
        position: 'right',
        splitLine: noSplitLine,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { ...axisLabelStyle, color: tone.muted, formatter: pctAxisFormatter },
      },
      dataZoom: zoom ? [
        { type: 'inside', start: zoomStart, end: 100 },
        { type: 'slider', start: zoomStart, end: 100, bottom: 30, height: 18, borderColor: tone.border, backgroundColor: tone.panel, textStyle: { color: tone.muted } },
      ] : [],
      series: chartSeries.map((item, index) => ({
        name: item.name,
        type: 'bar',
        barMaxWidth: 28,
        data: item.values,
        itemStyle: { color: item.color },
        markLine: index === 0 ? { ...zeroMarkLine, lineStyle: { ...zeroMarkLine.lineStyle, color: tone.border } } : undefined,
      })),
    },
    true,
  )
}

function renderLine() {
  if (!lineRef.value) return
  if (!lineChart) lineChart = echarts.init(lineRef.value)
  const tone = chartPalette()
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
        textStyle: { color: tone.ink, fontSize: 12 },
        data: ['累计盈亏', '清仓盈亏'],
      },
      grid: { left: compact ? 2 : 8, right: compact ? 38 : 72, top: 36, bottom: compact ? 22 : 28 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: tone.panel,
        borderColor: tone.border,
        textStyle: { color: tone.ink },
        valueFormatter: (v) => moneyAxisFormatter(v),
      },
      xAxis: {
        type: 'category',
        data: points.map((p) => p.date),
        boundaryGap: false,
        axisTick: { show: false },
        axisLine: { ...axisLineStyle, lineStyle: { color: tone.border } },
        axisLabel: {
          ...axisLabelStyle, color: tone.muted,
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
          ...axisLabelStyle, color: tone.muted,
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
          markLine: { ...zeroMarkLine, lineStyle: { ...zeroMarkLine.lineStyle, color: tone.border } },
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

watch(fxDirection, renderBar)
watch(isDark, () => {
  renderBar()
  renderLine()
})
watch(range, load)
onMounted(() => {
  load()
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  loadToken += 1
  benchmarkToken += 1
  window.removeEventListener('resize', onResize)
  barChart?.dispose()
  lineChart?.dispose()
  barChart = null
  lineChart = null
})
</script>

<template>
  <div class="analysis">
    <a-alert v-if="analysisError" type="error" :message="analysisError" show-icon class="analysis-error" />
    <a-spin :spinning="dataLoading">
      <section class="profitability-section">
        <div class="benchmark-heading">
          <div>
            <h2 class="section-title">投资组合盈利能力</h2>
            <p class="benchmark-subtitle">
              标普500 · 3个月美债 · 黄金 · 比特币 · 汇率
              <span v-if="benchmarks?.end_date">{{ benchmarks.start_date }} — {{ benchmarks.end_date }}</span>
            </p>
          </div>
          <div class="benchmark-actions">
            <label>汇率口径
              <select v-model="fxDirection" aria-label="汇率口径">
                <option value="usd_cny">美元兑人民币</option>
                <option value="cny_usd">人民币兑美元</option>
              </select>
            </label>
            <a-button :loading="benchmarkLoading" :disabled="dataLoading || !monthly.items?.length" @click="loadBenchmarks">刷新基准</a-button>
          </div>
        </div>
        <a-alert v-if="benchmarkError" type="error" :message="benchmarkError" show-icon class="analysis-error" />
        <a-spin :spinning="benchmarkLoading">
          <div ref="barRef" class="chart-box bar-chart" />
          <p v-if="!dataLoading && !monthly.items?.length" class="benchmark-empty">所选区间没有可用的已完成账本净值。</p>
        </a-spin>
        <div v-if="benchmarks" class="benchmark-status">
          <span v-for="item in monthlyBenchmarks.filter((series) => series.values.some((value) => value == null))" :key="item.key">
            {{ item.label }}：{{ item.values.every((value) => value == null) ? '缺少有效历史数据' : '部分月份数据缺失' }}
          </span>
        </div>
        <p class="benchmark-note">按各月实际起止日比较收益，首尾不足整月按实际区间计算。组合为 {{ currency }} 账本净值变动（包含出入金），QQQ、标普500、黄金和比特币为美元收益；仅比较，不进行币种换算。</p>
        <p class="benchmark-note">3个月美债按已发布年化利率逐日复利折算同期收益，不是年化利率本身；黄金使用 GLD ETF代理。{{ fxDirection === 'usd_cny' ? 'USD/CNY 正收益表示美元升值、人民币贬值。' : 'CNY/USD 正收益表示人民币升值、美元贬值。' }} 缺失数据留空，不填成零收益。</p>
        <div v-if="benchmarks" class="benchmark-sources">
          <details v-for="item in comparisonSeries" :key="item.key">
            <summary>{{ item.label }} · {{ item.source }}</summary>
            <p>{{ item.note }}</p>
          </details>
        </div>
      </section>

      <h2 class="section-title profit-title">获利</h2>
      <div ref="lineRef" class="chart-box line-chart" />
      <div class="profit-ranges">
        <button
          v-for="item in rangeOptions"
          :key="item.key"
          type="button"
          class="profit-chip"
          :class="{ active: range === item.key }"
          @click="range = item.key"
        >
          <div class="chip-label">{{ item.label }}</div>
          <div v-if="formatChipMoney(item.pnl)" class="chip-value" :class="pnlClass(item.pnl)">
            <span class="chip-num">{{ formatChipMoney(item.pnl).text }}</span>
            <span class="chip-ccy">{{ formatChipMoney(item.pnl).currency }}</span>
          </div>
          <div v-else class="chip-value muted">—</div>
        </button>
      </div>
    </a-spin>
  </div>
</template>

<style scoped>
.analysis {
  padding-top: 4px;
}

.analysis-error { margin-bottom: 12px; }
.benchmark-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}
.benchmark-subtitle {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.7;
}
.benchmark-subtitle span { display: block; }
.benchmark-actions { display: flex; align-items: center; gap: 10px; }
.benchmark-actions label { color: var(--muted); font-size: 12px; }
.benchmark-actions select {
  margin-left: 6px;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--panel);
  color: var(--text);
}
.benchmark-status { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 12px; color: #d9852a; font-size: 12px; }
.benchmark-empty { margin: 0; padding: 12px; text-align: center; color: var(--muted); }
.benchmark-note { margin: 10px 0 0; color: var(--muted); font-size: 12px; line-height: 1.7; }
.benchmark-sources { display: grid; gap: 6px; margin: 12px 0 0; color: var(--muted); font-size: 12px; }
.benchmark-sources summary { cursor: pointer; }
.benchmark-sources p { margin: 6px 0 0; line-height: 1.7; }

.section-title {
  margin: 0 0 8px;
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
  line-height: 1.3;
}

.profit-title {
  margin-top: 28px;
}

.chart-box {
  width: 100%;
}

.bar-chart {
  height: 320px;
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
  background: var(--chip);
}

.profit-chip.active {
  background: var(--chip);
}

.chip-label {
  font-size: 12px;
  color: var(--muted);
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
  color: var(--muted);
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
  .benchmark-actions { flex-wrap: wrap; }

  .section-title {
    font-size: 16px;
  }

  .profit-title {
    margin-top: 22px;
  }

  .bar-chart {
    height: 290px;
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
    border: 1px solid var(--border);
  }

  .profit-chip.active {
    border-color: #c7d2fe;
  }
}
</style>
