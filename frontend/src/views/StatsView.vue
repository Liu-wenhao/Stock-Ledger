<script setup>
import { computed, ref, watch } from 'vue'
import dayjs from 'dayjs'
import updateLocale from 'dayjs/plugin/updateLocale'
import 'dayjs/locale/zh-cn'
import locale from 'ant-design-vue/es/calendar/locale/zh_CN'
import { fetchDailyPnl } from '@/api/portfolio'
import { formatMoney, formatPct, pnlClass, toNumber } from '@/utils/format'

dayjs.extend(updateLocale)
dayjs.updateLocale('zh-cn', { weekStart: 0 })
dayjs.locale('zh-cn')

// Weekday headers/grid follow dayjs weekStart (Sunday).
const calendarLocale = locale

const GRANULARITY = [
  { key: 'day', label: '日' },
  { key: 'month', label: '月' },
  { key: 'year', label: '年' },
]

const loading = ref(false)
const granularity = ref('day')
const year = ref(new Date().getFullYear())
const month = ref(new Date().getMonth() + 1)
const calendarValue = ref(dayjs())
let syncingCalendar = false
const data = ref({
  granularity: 'day',
  year: year.value,
  month: month.value,
  years: [],
  months: [],
  items: [],
  summary: null,
})

const summary = computed(() => data.value.summary || {})

const calendarMode = computed(() => (granularity.value === 'month' ? 'year' : 'month'))

const pnlByDate = computed(() => {
  const map = new Map()
  for (const item of data.value.items || []) {
    map.set(String(item.date).slice(0, 10), item)
  }
  return map
})

const summaryCards = computed(() => {
  const s = summary.value
  const g = granularity.value
  const unit = g === 'day' ? '天' : g === 'month' ? '月' : '年'
  const totalLabel = g === 'day' ? '本月累计盈亏' : g === 'month' ? '年度累计盈亏' : '累计盈亏'
  const bestLabel = g === 'day' ? '最佳单日' : g === 'month' ? '最佳单月' : '最佳单年'
  const worstLabel = g === 'day' ? '最差单日' : g === 'month' ? '最差单月' : '最差单年'
  return [
    { label: totalLabel, value: s.total_pnl, format: 'money' },
    { label: `盈利${unit}数`, value: s.profit_days, format: 'count' },
    { label: `亏损${unit}数`, value: s.loss_days, format: 'count' },
    { label: bestLabel, value: s.best_day_pnl, format: 'money' },
    { label: worstLabel, value: s.worst_day_pnl, format: 'money' },
  ]
})

function cellBackground(pnl) {
  if (pnl == null || pnl === '') return {}
  const n = toNumber(pnl)
  if (n === 0) return { background: '#f5f6fa' }
  return {
    background: n > 0 ? 'rgba(38, 166, 154, 0.35)' : 'rgba(239, 83, 80, 0.35)',
  }
}

function pad2(n) {
  return String(n).padStart(2, '0')
}

function formatCardValue(card) {
  if (card.value == null || card.value === '') return '—'
  if (card.format === 'count') {
    const unit = granularity.value === 'day' ? '天' : granularity.value === 'month' ? '月' : '年'
    return `${card.value} ${unit}`
  }
  return formatMoney(card.value).replace(' USD', '')
}

function formatCellPnl(v) {
  if (v == null || v === '') return ''
  const n = toNumber(v)
  if (n === 0) return '0'
  const abs = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })
  return `${n > 0 ? '+' : '-'}${abs}`
}

function getDayItem(current) {
  return pnlByDate.value.get(current.format('YYYY-MM-DD'))
}

function getMonthItem(current) {
  const prefix = `${current.year()}-${pad2(current.month() + 1)}`
  return (data.value.items || []).find((i) => String(i.date).startsWith(prefix))
}

function isActiveMonth(current) {
  return current.month() + 1 === data.value.month && current.year() === data.value.year
}

function isOtherMonthDay(current) {
  return granularity.value === 'day' && !isActiveMonth(current)
}

function isWeekday(current) {
  const day = current.day()
  return day >= 1 && day <= 5
}

function shouldShowDayPnl(current) {
  return !isOtherMonthDay(current) && isWeekday(current) && !!getDayItem(current)
}

function dayCellBackground(current) {
  if (!shouldShowDayPnl(current)) return {}
  return cellBackground(getDayItem(current)?.pnl)
}

async function load() {
  loading.value = true
  try {
    const params = {
      granularity: granularity.value,
      year: year.value,
    }
    if (granularity.value === 'day') params.month = month.value
    data.value = await fetchDailyPnl(params)
    year.value = data.value.year
    if (data.value.month) month.value = data.value.month
    syncingCalendar = true
    calendarValue.value = dayjs(`${data.value.year}-${pad2(data.value.month || 1)}-01`)
    syncingCalendar = false
  } finally {
    loading.value = false
  }
}

function setGranularity(g) {
  if (granularity.value === g) return
  granularity.value = g
  load()
}

function onPanelChange(date) {
  const d = dayjs(date)
  const nextYear = d.year()
  const nextMonth = d.month() + 1
  if (nextYear === year.value && (granularity.value !== 'day' || nextMonth === month.value)) return
  year.value = nextYear
  if (granularity.value === 'day') month.value = nextMonth
  load()
}

function yearLabel(item) {
  return String(item.date).slice(0, 4)
}

function calendarHeaderTitle(value) {
  const d = dayjs(value)
  if (granularity.value === 'day') return `${d.year()}年${d.month() + 1}月`
  return `${d.year()}年`
}

function shiftCalendar(value, delta) {
  const unit = granularity.value === 'day' ? 'month' : 'year'
  return dayjs(value).add(delta, unit)
}

watch(calendarValue, (value) => {
  if (syncingCalendar) return
  onPanelChange(value)
})

load()
</script>

<template>
  <div class="stats-view">
    <div class="toolbar">
      <h2 class="section-title">盈亏统计</h2>
      <div class="granularity-toggle">
        <button
          v-for="g in GRANULARITY"
          :key="g.key"
          type="button"
          class="mode-btn"
          :class="{ active: granularity === g.key }"
          @click="setGranularity(g.key)"
        >
          {{ g.label }}
        </button>
      </div>
    </div>

    <div class="summary-grid">
      <div v-for="card in summaryCards" :key="card.label" class="summary-card">
        <div class="summary-label">{{ card.label }}</div>
        <div class="summary-value" :class="card.format === 'money' ? pnlClass(card.value) : ''">
          {{ formatCardValue(card) }}
        </div>
      </div>
    </div>

    <a-spin :spinning="loading">
      <a-calendar
        v-if="granularity !== 'year'"
        v-model:value="calendarValue"
        :mode="calendarMode"
        :locale="calendarLocale"
        :fullscreen="false"
        class="pnl-calendar"
      >
        <template #headerRender="{ value, onChange }">
          <div class="calendar-header">
            <a-button size="small" type="text" @click="onChange(shiftCalendar(value, -1))">‹</a-button>
            <span class="calendar-title">{{ calendarHeaderTitle(value) }}</span>
            <a-button size="small" type="text" @click="onChange(shiftCalendar(value, 1))">›</a-button>
          </div>
        </template>

        <template #dateFullCellRender="{ current }">
          <div v-if="granularity === 'day'" class="calendar-cell day-cell">
            <div
              class="cell-body"
              :class="{
                'other-month': isOtherMonthDay(current),
                'cell-body--empty':
                  !isOtherMonthDay(current) && (!isWeekday(current) || !getDayItem(current)),
              }"
              :style="dayCellBackground(current)"
            >
              <div class="cell-date">{{ current.date() }}</div>
              <template v-if="shouldShowDayPnl(current)">
                <div class="cell-pnl" :class="pnlClass(getDayItem(current)?.pnl)">
                  {{ formatCellPnl(getDayItem(current)?.pnl) }}
                </div>
                <div class="cell-pct" :class="pnlClass(getDayItem(current)?.pnl)">
                  {{ formatPct(getDayItem(current)?.pnl_percent) }}
                </div>
              </template>
            </div>
          </div>
        </template>

        <template #monthFullCellRender="{ current }">
          <div v-if="granularity === 'month'" class="calendar-cell month-cell">
            <div
              v-if="getMonthItem(current)"
              class="cell-body"
              :style="cellBackground(getMonthItem(current)?.pnl)"
            >
              <div class="cell-month">{{ current.month() + 1 }}月</div>
              <div class="cell-pnl" :class="pnlClass(getMonthItem(current)?.pnl)">
                {{ formatCellPnl(getMonthItem(current)?.pnl) }}
              </div>
              <div class="cell-pct" :class="pnlClass(getMonthItem(current)?.pnl)">
                {{ formatPct(getMonthItem(current)?.pnl_percent) }}
              </div>
            </div>
            <div v-else class="cell-body cell-body--empty">
              <div class="cell-month">{{ current.month() + 1 }}月</div>
              <div class="cell-empty">—</div>
            </div>
          </div>
        </template>
      </a-calendar>

      <div v-else class="year-grid">
        <div v-for="item in data.items || []" :key="item.date" class="year-cell">
          <div class="cell-body" :style="cellBackground(item.pnl)">
            <div class="cell-year">{{ yearLabel(item) }}</div>
            <div class="cell-pnl" :class="pnlClass(item.pnl)">
              {{ formatMoney(item.pnl).replace(' USD', '') }}
            </div>
            <div class="cell-pct" :class="pnlClass(item.pnl)">
              {{ formatPct(item.pnl_percent) }}
            </div>
          </div>
        </div>
        <a-empty v-if="!(data.items || []).length" description="暂无统计数据" />
      </div>

      <a-empty
        v-if="granularity !== 'year' && !loading && !(data.items || []).length"
        description="暂无统计数据"
        class="calendar-empty"
      />
    </a-spin>

    <p class="hint">
      <template v-if="granularity === 'day'">按日查看：按美东交易日（周一至周五）统计盈亏，周末不展示。</template>
      <template v-else-if="granularity === 'month'">按月查看：切换至年面板，每格显示该月合计盈亏。</template>
      <template v-else>按年查看：各年度盈亏汇总。</template>
      绿色为盈利，红色为亏损。
    </p>
  </div>
</template>

<style scoped>
.stats-view {
  min-height: 420px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}

.section-title {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: #131722;
}

.granularity-toggle {
  display: inline-flex;
  gap: 4px;
  padding: 4px;
  background: #f0f3fa;
  border-radius: 8px;
}

.mode-btn {
  border: none;
  background: transparent;
  color: #787b86;
  font-size: 13px;
  font-weight: 600;
  padding: 4px 12px;
  border-radius: 6px;
  cursor: pointer;
}

.mode-btn.active {
  background: #131722;
  color: #fff;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}

.summary-card {
  background: #fff;
  border: 1px solid #e0e3eb;
  border-radius: 8px;
  padding: 12px 14px;
}

.summary-label {
  font-size: 12px;
  color: #787b86;
  margin-bottom: 6px;
}

.summary-value {
  font-size: 18px;
  font-weight: 700;
  color: #131722;
}

.pnl-calendar {
  background: #fff;
  border: 1px solid #e0e3eb;
  border-radius: 8px;
  padding: 8px 12px 12px;
}

.pnl-calendar :deep(.ant-picker-calendar-header) {
  padding: 0;
  border-bottom: none;
}

.calendar-header {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 8px 0 12px;
}

.calendar-title {
  min-width: 120px;
  text-align: center;
  font-size: 15px;
  font-weight: 700;
  color: #131722;
}

.pnl-calendar :deep(.ant-picker-calendar-date) {
  margin: 0;
  padding: 0;
  border: none;
  background: transparent;
}

.pnl-calendar :deep(.ant-picker-cell) {
  padding: 4px;
}

.pnl-calendar :deep(.ant-picker-cell-inner) {
  padding: 0;
  background: transparent !important;
  border: none !important;
}

.pnl-calendar :deep(.ant-picker-cell-selected .ant-picker-cell-inner) {
  background: transparent !important;
}

.pnl-calendar :deep(.ant-picker-calendar-date-value) {
  display: none;
}

.pnl-calendar :deep(.ant-picker-calendar-date-content) {
  height: auto;
  min-height: 88px;
}

.pnl-calendar :deep(.ant-picker-calendar-month-panel .ant-picker-cell) {
  padding: 6px;
}

.pnl-calendar :deep(.ant-picker-calendar-month-panel .ant-picker-calendar-date-content) {
  min-height: 104px;
}

.pnl-calendar :deep(.ant-picker-content th) {
  font-size: 14px;
}

.calendar-cell {
  min-height: 88px;
  display: flex;
  flex-direction: column;
  font-size: 14px;
}

.cell-body {
  flex: 1;
  width: 100%;
  min-height: 88px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 6px 4px;
  border-radius: 8px;
  text-align: center;
  box-sizing: border-box;
}

.cell-body.other-month {
  opacity: 0.38;
  background: #f5f6fa;
}

.cell-body--empty {
  background: #f5f6fa;
}

.day-cell .cell-date {
  font-size: 14px;
  font-weight: 600;
  color: #131722;
  line-height: 1.2;
}

.month-cell {
  min-height: 104px;
  text-align: center;
  font-size: 14px;
}

.month-cell .cell-month {
  font-size: 14px;
  font-weight: 700;
  color: #131722;
  line-height: 1.2;
}

.cell-year {
  font-size: 14px;
  font-weight: 700;
  color: #131722;
  line-height: 1.2;
}

.cell-pnl {
  font-size: 16px;
  font-weight: 700;
  line-height: 1.25;
  word-break: break-all;
}

.cell-empty {
  font-size: 14px;
  color: #b2b5be;
  padding: 8px 0;
}

.cell-pct {
  font-size: 14px;
  font-weight: 600;
  line-height: 1.2;
  opacity: 0.92;
}

.year-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;
}

.year-cell {
  padding: 0;
  background: transparent;
  border: none;
}

.year-cell .cell-body {
  min-height: 96px;
  border: 1px solid #e0e3eb;
}

.calendar-empty {
  margin-top: 24px;
}

.hint {
  margin: 12px 0 0;
  font-size: 12px;
  color: #787b86;
}

@media (max-width: 960px) {
  .summary-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .summary-value {
    font-size: 16px;
  }

  .pnl-calendar {
    padding: 0;
  }

  .pnl-calendar :deep(.ant-picker-cell) {
    padding: 0;
  }

  .pnl-calendar :deep(.ant-picker-cell-inner) {
    width: 100%;
    height: 100%;
  }

  .pnl-calendar :deep(.ant-picker-content) {
    border-collapse: collapse;
  }

  .pnl-calendar :deep(.ant-picker-calendar-month-panel .ant-picker-cell) {
    padding: 0;
  }

  .pnl-calendar :deep(.ant-picker-content th) {
    font-size: 10px;
  }

  .pnl-calendar :deep(.ant-picker-calendar-date-content) {
    min-height: 56px;
  }

  .pnl-calendar :deep(.ant-picker-calendar-month-panel .ant-picker-calendar-date-content) {
    min-height: 64px;
  }

  .calendar-cell {
    min-height: 56px;
    height: 100%;
    font-size: 10px;
  }

  .month-cell {
    min-height: 64px;
    font-size: 10px;
  }

  .cell-body {
    min-height: 56px;
    height: 100%;
    padding: 2px 1px;
    border-radius: 0;
    gap: 4px;
  }

  .month-cell .cell-body {
    min-height: 64px;
  }

  .day-cell .cell-date,
  .month-cell .cell-month,
  .cell-year,
  .cell-empty {
    font-size: 10px;
  }

  .cell-pnl {
    font-size: 11px;
  }

  .cell-pct {
    font-size: 9px;
  }

  .year-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0;
  }

  .year-cell .cell-body {
    min-height: 72px;
    border-radius: 0;
    border-left: none;
    border-top: none;
  }
}
</style>
