<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { fetchHoldings } from '@/api/portfolio'
import SymbolCell from '@/components/SymbolCell.vue'
import { formatMoney, formatPct, formatQty, pnlClass, toNumber } from '@/utils/format'
import { useTableScrollY } from '@/composables/useTableScroll'

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
const tableScroll = computed(() => ({ x: 1200, y: tableScrollY.value }))
const pagination = reactive({
  current: 1,
  pageSize: 20,
  showSizeChanger: true,
  showTotal: (total) => `共 ${total} 条`,
  pageSizeOptions: ['10', '20', '50', '100'],
})
let chart

function onTableChange(pag) {
  pagination.current = pag.current
  pagination.pageSize = pag.pageSize
}

const reserved = ['financials', 'risk', 'technical']

async function load() {
  loading.value = true
  try {
    data.value = await fetchHoldings({
      view: subTab.value,
      include_closed: includeClosed.value,
      q: keyword.value || undefined,
    })
    if (subTab.value === 'performance') renderPerf()
  } finally {
    loading.value = false
  }
}

const positionColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 220 },
  { title: '分配 %', dataIndex: 'allocation', key: 'allocation', align: 'right' },
  { title: '数量', dataIndex: 'quantity', key: 'quantity', align: 'right' },
  { title: '平均价格', dataIndex: 'avg_price', key: 'avg_price', align: 'right' },
  { title: '目前收盘价', dataIndex: 'last_price', key: 'last_price', align: 'right' },
  { title: '成本值', dataIndex: 'cost_basis', key: 'cost_basis', align: 'right' },
  { title: '值', dataIndex: 'market_value', key: 'market_value', align: 'right' },
  { title: '累计盈亏', dataIndex: 'total_pnl', key: 'total_pnl', align: 'right' },
  { title: '累计盈亏 %', dataIndex: 'total_pnl_percent', key: 'total_pnl_percent', align: 'right' },
]

const priceColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 220 },
  { title: '目前收盘价', dataIndex: 'last_price', key: 'last_price', align: 'right' },
  { title: '日涨跌', dataIndex: 'day_change', key: 'day_change', align: 'right' },
  { title: '日涨跌 %', dataIndex: 'day_change_percent', key: 'day_change_percent', align: 'right' },
  { title: '开盘', dataIndex: 'open_price', key: 'open_price', align: 'right' },
  { title: '最高', dataIndex: 'high_price', key: 'high_price', align: 'right' },
  { title: '最低', dataIndex: 'low_price', key: 'low_price', align: 'right' },
  { title: '昨收', dataIndex: 'prev_close', key: 'prev_close', align: 'right' },
]

const columns = computed(() => (subTab.value === 'price' ? priceColumns : positionColumns))

const positionsSummary = computed(() => {
  // Sum currently listed rows. With「显示已清仓」off this matches KPI 持仓盈亏;
  // with it on, totals equal 累计盈亏 (持仓+清仓).
  // % = 盈亏合计 / 市值合计（与 KPI 持仓盈亏% 同口径）
  const items = data.value.items || []
  const sumKey = (key) => items.reduce((acc, row) => acc + toNumber(row[key]), 0)
  const pnl = sumKey('total_pnl')
  const marketValue = sumKey('market_value')
  return {
    allocation: sumKey('allocation'),
    quantity: sumKey('quantity'),
    cost_basis: sumKey('cost_basis'),
    market_value: marketValue,
    total_pnl: pnl,
    total_pnl_percent: marketValue > 0 ? (pnl / marketValue) * 100 : 0,
  }
})

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
onMounted(load)
</script>

<template>
  <div>
    <a-tabs v-model:activeKey="subTab" size="small">
      <a-tab-pane key="positions" tab="持仓" />
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

    <template v-else>
      <div class="toolbar">
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="load"
        />
        <div style="display: flex; gap: 12px; align-items: center">
          <span>显示已清仓</span>
          <a-switch v-model:checked="includeClosed" />
        </div>
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
          <template v-else-if="column.key === 'allocation'">{{ formatPct(record.allocation).replace('+', '') }}</template>
          <template v-else-if="column.key === 'quantity'">{{ formatQty(record.quantity) }}</template>
          <template v-else-if="['avg_price', 'last_price', 'open_price', 'high_price', 'low_price', 'prev_close'].includes(column.key)">
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
                {{ formatPct(positionsSummary.allocation).replace('+', '') }}
              </a-table-summary-cell>
              <a-table-summary-cell :index="2" align="right">
                {{ formatQty(positionsSummary.quantity) }}
              </a-table-summary-cell>
              <a-table-summary-cell :index="3" align="right">—</a-table-summary-cell>
              <a-table-summary-cell :index="4" align="right">—</a-table-summary-cell>
              <a-table-summary-cell :index="5" align="right">
                {{ formatMoney(positionsSummary.cost_basis) }}
              </a-table-summary-cell>
              <a-table-summary-cell :index="6" align="right">
                {{ formatMoney(positionsSummary.market_value) }}
              </a-table-summary-cell>
              <a-table-summary-cell :index="7" align="right">
                <span :class="pnlClass(positionsSummary.total_pnl)">
                  {{ formatMoney(positionsSummary.total_pnl) }}
                </span>
              </a-table-summary-cell>
              <a-table-summary-cell :index="8" align="right">
                <span :class="pnlClass(positionsSummary.total_pnl_percent)">
                  {{ formatPct(positionsSummary.total_pnl_percent) }}
                </span>
              </a-table-summary-cell>
            </a-table-summary-row>
          </a-table-summary>
        </template>
      </a-table>
    </template>
  </div>
</template>

<style scoped>
.holdings-summary-row :deep(td) {
  font-weight: 600;
  background: #f7f8fa !important;
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
</style>
