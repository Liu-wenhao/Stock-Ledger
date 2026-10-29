<script setup>
import { computed, ref, watch } from 'vue'
import dayjs from 'dayjs'
import updateLocale from 'dayjs/plugin/updateLocale'
import 'dayjs/locale/zh-cn'
import locale from 'ant-design-vue/es/calendar/locale/zh_CN'
import { fetchDailyHoldings, fetchDailyPnl, fetchPeriodHoldings } from '@/api/portfolio'
import SymbolCell from '@/components/SymbolCell.vue'
import { formatMoney, formatPct, pnlClass, toNumber } from '@/utils/format'

dayjs.extend(updateLocale)
dayjs.updateLocale('zh-cn', { weekStart: 0 })
dayjs.locale('zh-cn')

// Weekday headers/grid follow dayjs weekStart (Sunday).
const calendarLocale = locale

const GRANULARITY = [
  { key: 'day', label: '日' },
  { key: 'month', label: '月' },
  { key: 'quarter', label: '季' },
  { key: 'year', label: '年' },
]

const PERIOD_UNIT = { day: '天', month: '月', quarter: '季', year: '年' }
const PERIOD_TITLE = {
  day: '当日个股盈亏',
  month: '当月个股盈亏',
  quarter: '当季个股盈亏',
  year: '当年个股盈亏',
}
const PERIOD_NET = {
  day: '当日净盈亏',
  month: '当月净盈亏',
  quarter: '当季净盈亏',
  year: '当年净盈亏',
}

const loading = ref(false)
const granularity = ref('day')
const year = ref(new Date().getFullYear())
const month = ref(new Date().getMonth() + 1)
const calendarValue = ref(dayjs())
let syncingCalendar = false
const selectedDate = ref(null)
const selectedPeriod = ref(null)
const dailyHoldingsLoading = ref(false)
const dailyHoldingsError = ref('')
const dailyHoldings = ref({
  date: null,
  total_gross_pnl: 0,
  total_commission: 0,
  total_pnl: 0,
  items: [],
})
let dailyHoldingsRequestId = 0

const dailyMoverPool = computed(() =>
  (dailyHoldings.value.items || []).filter((item) => toNumber(item.day_pnl) !== 0),
)
const dailyWinners = computed(() =>
  [...dailyMoverPool.value]
    .filter((item) => toNumber(item.day_pnl) > 0)
    .sort((a, b) => toNumber(b.day_pnl) - toNumber(a.day_pnl)),
)
const dailyLosers = computed(() =>
  [...dailyMoverPool.value]
    .filter((item) => toNumber(item.day_pnl) < 0)
    .sort((a, b) => toNumber(a.day_pnl) - toNumber(b.day_pnl)),
)
const dailyMovements = computed(() =>
  [...dailyMoverPool.value].sort(
    (a, b) => Math.abs(toNumber(b.day_pnl)) - Math.abs(toNumber(a.day_pnl)),
  ),
)

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
  const unit = PERIOD_UNIT[g] || '天'
  const totalLabel = g === 'day' ? '本月累计盈亏' : g === 'year' ? '累计盈亏' : '年度累计盈亏'
  const bestLabel = `最佳单${unit === '天' ? '日' : unit}`
  const worstLabel = `最差单${unit === '天' ? '日' : unit}`
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
    return `${card.value} ${PERIOD_UNIT[granularity.value] || '天'}`
  }
  return formatMoney(card.value).replace(/ [A-Z]{3}$/, '')
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

function isSelectedDay(current) {
  return granularity.value === 'day' && selectedDate.value === current.format('YYYY-MM-DD')
}

function isSelectedMonth(current) {
  if (granularity.value !== 'month' || !selectedPeriod.value) return false
  return selectedPeriod.value.key === `${current.year()}-${pad2(current.month() + 1)}`
}

function quarterOf(item) {
  const month = Number(String(item?.date).slice(5, 7))
  if (!month) return null
  return Math.floor((month - 1) / 3) + 1
}

function quarterItem(quarter) {
  return (data.value.items || []).find((item) => quarterOf(item) === quarter)
}

function isSelectedQuarter(quarter) {
  return granularity.value === 'quarter' && selectedPeriod.value?.key === `${year.value}-Q${quarter}`
}

function isSelectedYear(item) {
  return granularity.value === 'year' && selectedPeriod.value?.key === String(item.date).slice(0, 4)
}

function formatDailyDate(value) {
  if (!value) return '请选择日期'
  const parsed = dayjs(value)
  return parsed.isValid() ? parsed.format('YYYY年M月D日') : String(value)
}

function dailyStatusClass(status) {
  if (status === '持仓') return 'daily-status--holding'
  if (status === '日内交易') return 'daily-status--intraday'
  return 'daily-status--closed'
}


function dailyBarWidth(item, peers) {
  const value = Math.abs(toNumber(item.day_pnl))
  const max = Math.max(...(peers || []).map((peer) => Math.abs(toNumber(peer.day_pnl))), value, 1)
  return `${Math.max((value / max) * 100, 8)}%`
}

function clearDailyHoldings() {
  dailyHoldingsRequestId += 1
  selectedDate.value = null
  selectedPeriod.value = null
  dailyHoldingsLoading.value = false
  dailyHoldingsError.value = ''
  dailyHoldings.value = {
    date: null,
    total_gross_pnl: 0,
    total_commission: 0,
    total_pnl: 0,
    items: [],
  }
}

function monthPeriod(yearValue, monthValue) {
  const start = dayjs(`${yearValue}-${pad2(monthValue)}-01`)
  return {
    key: `${yearValue}-${pad2(monthValue)}`,
    start: start.format('YYYY-MM-DD'),
    end: start.endOf('month').format('YYYY-MM-DD'),
    label: `${yearValue}年${monthValue}月`,
  }
}

function quarterPeriod(yearValue, quarter) {
  const startMonth = (quarter - 1) * 3 + 1
  const start = dayjs(`${yearValue}-${pad2(startMonth)}-01`)
  return {
    key: `${yearValue}-Q${quarter}`,
    start: start.format('YYYY-MM-DD'),
    end: start.add(2, 'month').endOf('month').format('YYYY-MM-DD'),
    label: `${yearValue}年第${quarter}季度`,
  }
}

function yearPeriod(yearValue) {
  return {
    key: String(yearValue),
    start: `${yearValue}-01-01`,
    end: `${yearValue}-12-31`,
    label: `${yearValue}年`,
  }
}

function dayPeriod(dateValue) {
  const parsed = dayjs(dateValue)
  return {
    key: parsed.format('YYYY-MM-DD'),
    start: parsed.format('YYYY-MM-DD'),
    end: parsed.format('YYYY-MM-DD'),
    label: parsed.format('YYYY年M月D日'),
  }
}

async function loadPeriodHoldings(period) {
  const requestId = ++dailyHoldingsRequestId
  selectedPeriod.value = period
  if (granularity.value === 'day') selectedDate.value = period.start
  dailyHoldingsLoading.value = true
  dailyHoldingsError.value = ''
  try {
    const result = period.start === period.end
      ? await fetchDailyHoldings(period.start)
      : await fetchPeriodHoldings(period.start, period.end)
    if (requestId === dailyHoldingsRequestId) {
      dailyHoldings.value = result
      if (granularity.value === 'day') {
        selectedDate.value = String(result.date).slice(0, 10)
        selectedPeriod.value = dayPeriod(selectedDate.value)
      }
    }
  } catch {
    if (requestId === dailyHoldingsRequestId) {
      dailyHoldingsError.value = '个股盈亏加载失败，请稍后重试。'
      dailyHoldings.value = {
        date: period.start,
        total_gross_pnl: 0,
        total_commission: 0,
        total_pnl: 0,
        items: [],
      }
    }
  } finally {
    if (requestId === dailyHoldingsRequestId) dailyHoldingsLoading.value = false
  }
}

async function loadDailyHoldings(dateValue) {
  await loadPeriodHoldings(dayPeriod(dateValue))
}

async function selectLatestDay() {
  const items = [...(data.value.items || [])].sort((a, b) =>
    String(a.date).localeCompare(String(b.date)),
  )
  const latest = items[items.length - 1]
  if (!latest) {
    clearDailyHoldings()
    return
  }
  selectedDate.value = String(latest.date).slice(0, 10)
  await loadDailyHoldings(selectedDate.value)
}

async function selectDefaultPeriod() {
  const items = [...(data.value.items || [])].sort((a, b) =>
    String(a.date).localeCompare(String(b.date)),
  )
  const latest = items[items.length - 1]
  if (!latest) {
    clearDailyHoldings()
    return
  }
  const g = granularity.value
  if (g === 'month') {
    const parsed = dayjs(latest.date)
    await loadPeriodHoldings(monthPeriod(parsed.year(), parsed.month() + 1))
    return
  }
  if (g === 'quarter') {
    await loadPeriodHoldings(quarterPeriod(year.value, quarterOf(latest) || 1))
    return
  }
  if (g === 'year') {
    const match = items.find((item) => String(item.date).startsWith(String(year.value))) || latest
    await loadPeriodHoldings(yearPeriod(Number(String(match.date).slice(0, 4))))
  }
}

async function onSelectDate(value) {
  const parsed = dayjs(value)
  if (granularity.value === 'day') {
    await loadDailyHoldings(parsed.format('YYYY-MM-DD'))
    return
  }
  if (granularity.value === 'month') {
    await loadPeriodHoldings(monthPeriod(parsed.year(), parsed.month() + 1))
  }
}

function selectQuarter(quarter) {
  loadPeriodHoldings(quarterPeriod(year.value, quarter))
}

function selectYear(item) {
  loadPeriodHoldings(yearPeriod(Number(String(item.date).slice(0, 4))))
}

function shiftYear(delta) {
  year.value += delta
  load()
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
    if (granularity.value === 'day') await selectLatestDay()
    else await selectDefaultPeriod()
  } finally {
    loading.value = false
  }
}

function setGranularity(g) {
  if (granularity.value === g) return
  granularity.value = g
  clearDailyHoldings()
  load()
}

function onPanelChange(date) {
  const d = dayjs(date)
  const nextYear = d.year()
  const nextMonth = d.month() + 1
  if (nextYear === year.value && (granularity.value !== 'day' || nextMonth === month.value)) return
  year.value = nextYear
  if (granularity.value === 'day') month.value = nextMonth
  clearDailyHoldings()
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
        v-if="granularity === 'day' || granularity === 'month'"
        v-model:value="calendarValue"
        :mode="calendarMode"
        :locale="calendarLocale"
        :fullscreen="false"
        class="pnl-calendar"
        @select="onSelectDate"
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
                'cell-body--selected': isSelectedDay(current),
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
              :class="{ 'cell-body--selected': isSelectedMonth(current) }"
              :style="cellBackground(getMonthItem(current)?.pnl)"
              @click="loadPeriodHoldings(monthPeriod(current.year(), current.month() + 1))"
            >
              <div class="cell-month">{{ current.month() + 1 }}月</div>
              <div class="cell-pnl" :class="pnlClass(getMonthItem(current)?.pnl)">
                {{ formatCellPnl(getMonthItem(current)?.pnl) }}
              </div>
              <div class="cell-pct" :class="pnlClass(getMonthItem(current)?.pnl)">
                {{ formatPct(getMonthItem(current)?.pnl_percent) }}
              </div>
            </div>
            <div
              v-else
              class="cell-body cell-body--empty"
              :class="{ 'cell-body--selected': isSelectedMonth(current) }"
              @click="loadPeriodHoldings(monthPeriod(current.year(), current.month() + 1))"
            >
              <div class="cell-month">{{ current.month() + 1 }}月</div>
              <div class="cell-empty">—</div>
            </div>
          </div>
        </template>
      </a-calendar>

      <div v-else-if="granularity === 'quarter'" class="quarter-panel">
        <div class="calendar-header">
          <a-button size="small" type="text" @click="shiftYear(-1)">‹</a-button>
          <span class="calendar-title">{{ year }}年</span>
          <a-button size="small" type="text" @click="shiftYear(1)">›</a-button>
        </div>
        <div class="year-grid quarter-grid">
          <button
            v-for="quarter in 4"
            :key="quarter"
            type="button"
            class="year-cell"
            @click="selectQuarter(quarter)"
          >
            <div
              class="cell-body"
              :class="{
                'cell-body--empty': !quarterItem(quarter),
                'cell-body--selected': isSelectedQuarter(quarter),
              }"
              :style="cellBackground(quarterItem(quarter)?.pnl)"
            >
              <div class="cell-year">第{{ quarter }}季度</div>
              <template v-if="quarterItem(quarter)">
                <div class="cell-pnl" :class="pnlClass(quarterItem(quarter)?.pnl)">
                  {{ formatCellPnl(quarterItem(quarter)?.pnl) }}
                </div>
                <div class="cell-pct" :class="pnlClass(quarterItem(quarter)?.pnl)">
                  {{ formatPct(quarterItem(quarter)?.pnl_percent) }}
                </div>
              </template>
              <div v-else class="cell-empty">—</div>
            </div>
          </button>
        </div>
      </div>

      <div v-else class="year-grid">
        <button
          v-for="item in data.items || []"
          :key="item.date"
          type="button"
          class="year-cell"
          @click="selectYear(item)"
        >
          <div
            class="cell-body"
            :class="{ 'cell-body--selected': isSelectedYear(item) }"
            :style="cellBackground(item.pnl)"
          >
            <div class="cell-year">{{ yearLabel(item) }}</div>
            <div class="cell-pnl" :class="pnlClass(item.pnl)">
              {{ formatMoney(item.pnl).replace(/ [A-Z]{3}$/, '') }}
            </div>
            <div class="cell-pct" :class="pnlClass(item.pnl)">
              {{ formatPct(item.pnl_percent) }}
            </div>
          </div>
        </button>
        <a-empty v-if="!(data.items || []).length" description="暂无统计数据" />
      </div>

      <a-empty
        v-if="(granularity === 'day' || granularity === 'month') && !loading && !(data.items || []).length"
        description="暂无统计数据"
        class="calendar-empty"
      />
    </a-spin>

    <section v-if="selectedPeriod" class="daily-holdings-card">
      <div class="daily-holdings-header">
        <div>
          <div class="daily-holdings-title-row">
            <h3 class="daily-holdings-title">{{ PERIOD_TITLE[granularity] }}</h3>
            <a-tag class="daily-count-tag">{{ (dailyHoldings.items || []).length }} 只</a-tag>
          </div>
          <p class="daily-holdings-subtitle">
            {{ selectedPeriod.label }} · 毛盈亏扣除区间手续费后为净盈亏
          </p>
        </div>
        <div class="daily-total">
          <div class="daily-total-main">
            <span>{{ PERIOD_NET[granularity] }}</span>
            <strong :class="pnlClass(dailyHoldings.total_pnl)">
              {{ formatMoney(dailyHoldings.total_pnl) }}
            </strong>
          </div>
          <div class="daily-total-breakdown">
            <div>
              <span>毛盈亏</span>
              <b :class="pnlClass(dailyHoldings.total_gross_pnl)">
                {{ formatMoney(dailyHoldings.total_gross_pnl) }}
              </b>
            </div>
            <div>
              <span>手续费</span>
              <b class="daily-commission">{{ formatMoney(dailyHoldings.total_commission) }}</b>
            </div>
          </div>
        </div>
      </div>

      <a-alert
        v-if="dailyHoldingsError"
        type="error"
        show-icon
        :message="dailyHoldingsError"
        class="daily-holdings-alert"
      >
        <template #action>
          <a-button size="small" @click="loadPeriodHoldings(selectedPeriod)">重试</a-button>
        </template>
      </a-alert>

      <a-spin :spinning="dailyHoldingsLoading">
        <div v-if="!(dailyHoldings.items || []).length" class="daily-mover-empty">
          <a-empty :description="dailyHoldingsLoading ? '正在加载…' : '该区间没有持仓或交易股票'" />
        </div>
        <div v-else class="daily-movers">
          <section class="daily-mover-panel">
            <div class="daily-mover-panel-header">
              <div>
                <h4>个股明细</h4>
                <p>按盈亏绝对值排序</p>
              </div>
              <div class="daily-mover-counts">
                <span class="up">盈利 {{ dailyWinners.length }}</span>
                <span class="down">亏损 {{ dailyLosers.length }}</span>
              </div>
            </div>

            <div
              v-for="record in dailyMovements"
              :key="record.symbol"
              class="daily-mover-row"
            >
              <SymbolCell :symbol="record.symbol" :name="record.name" :logo-url="record.logo_url" />
              <div class="daily-mover-overview">
                <div class="daily-mover-track">
                  <span
                    class="daily-mover-fill"
                    :class="toNumber(record.day_pnl) > 0 ? 'up' : 'down'"
                    :style="{ width: dailyBarWidth(record, dailyMovements) }"
                  />
                </div>
                <div class="daily-mover-meta">
                  <span v-if="record.close_price != null">收盘 {{ formatMoney(record.close_price) }}</span>
                  <span class="daily-commission">费 {{ formatMoney(record.commission) }}</span>
                  <a-tag v-if="record.status !== '持仓'" :class="dailyStatusClass(record.status)">{{ record.status }}</a-tag>
                </div>
              </div>
              <div class="daily-mover-value">
                <strong :class="pnlClass(record.day_pnl)">{{ formatMoney(record.day_pnl) }}</strong>
                <span :class="pnlClass(record.day_pnl_percent)">
                  {{ formatPct(record.day_pnl_percent) }}
                </span>
              </div>
            </div>
          </section>
        </div>
      </a-spin>
    </section>

    <p class="hint">
      <template v-if="granularity === 'day'">按日查看：按美东交易日（周一至周五）统计净盈亏，已扣除当日手续费，周末不展示。</template>
      <template v-else-if="granularity === 'month'">按月查看：点击月份，下方显示该月每只股票的盈亏。</template>
      <template v-else-if="granularity === 'quarter'">按季查看：点击季度，下方显示该季每只股票的盈亏。</template>
      <template v-else>按年查看：点击年份，下方显示该年每只股票的盈亏。</template>
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
  position: relative;
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
  transition:
    box-shadow 0.18s ease,
    transform 0.18s ease,
    filter 0.18s ease;
}

.cell-body.other-month {
  opacity: 0.38;
  background: #f5f6fa;
}

.cell-body--empty {
  background: #f5f6fa;
}

.cell-body--selected {
  z-index: 1;
  border: 1.5px solid rgba(107, 114, 128, 0.62);
  box-shadow: 0 7px 18px rgba(75, 85, 99, 0.2);
}

@media (hover: hover) {
  .pnl-calendar :deep(.ant-picker-cell:not(.ant-picker-cell-disabled) .cell-body:not(.other-month)):hover:not(.cell-body--selected) {
    transform: translateY(-1px);
    filter: brightness(0.98);
  }
}

.daily-holdings-card {
  margin-top: 20px;
  padding: 20px;
  overflow: hidden;
  background:
    radial-gradient(circle at 100% 0, rgba(41, 98, 255, 0.035), transparent 28%),
    #fff;
  border: 1px solid #e0e3eb;
  border-radius: 12px;
  box-shadow: 0 10px 28px rgba(19, 23, 34, 0.055);
}

.daily-holdings-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 16px;
}

.daily-holdings-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.daily-holdings-title {
  margin: 0;
  color: #131722;
  font-size: 17px;
  font-weight: 750;
  letter-spacing: -0.01em;
}

.daily-count-tag {
  margin: 0;
  color: #4e5b73;
  background: #f2f4f8;
  border-color: #e2e6ee;
}

.daily-holdings-subtitle {
  margin: 5px 0 0;
  color: #737986;
  font-size: 12px;
}

.daily-total {
  display: grid;
  grid-template-columns: minmax(142px, 1fr) auto;
  align-items: center;
  gap: 20px;
  min-width: 380px;
  padding: 11px 14px;
  background: #f8f9fb;
  border: 1px solid #e9edf3;
  border-radius: 10px;
}

.daily-total-main,
.daily-total-breakdown > div {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.daily-total-main strong {
  color: #131722;
  font-size: 20px;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}

.daily-total span {
  color: #787b86;
  font-size: 11px;
}

.daily-total-breakdown {
  display: flex;
  gap: 18px;
  padding-left: 18px;
  border-left: 1px solid #e4e8ef;
}

.daily-total-breakdown b {
  color: #4e5b73;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.daily-total-breakdown b.daily-commission,
.daily-commission {
  color: #8a5a00;
  font-variant-numeric: tabular-nums;
}

.daily-holdings-alert {
  margin-bottom: 14px;
}

.daily-movers {
  display: block;
}

.daily-mover-panel {
  min-width: 0;
  width: 100%;
}

.daily-mover-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 2px;
  padding: 0 4px 10px;
  border-bottom: 1px solid #eef1f5;
}

.daily-mover-panel-header h4 {
  margin: 0;
  color: #272b35;
  font-size: 13px;
  font-weight: 750;
}

.daily-mover-panel-header p {
  margin: 3px 0 0;
  color: #9aa0aa;
  font-size: 10px;
}

.daily-mover-counts {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 11px;
  font-weight: 650;
}

.daily-mover-counts .up {
  color: #089981;
}

.daily-mover-counts .down {
  color: #f23645;
}

.daily-mover-row {
  display: grid;
  grid-template-columns: minmax(170px, 0.85fr) minmax(240px, 2fr) 110px;
  align-items: center;
  gap: 18px;
  min-height: 70px;
  padding: 10px 4px;
  border-bottom: 1px solid #f0f2f6;
  transition: background 0.15s ease;
}

.daily-mover-row:last-child {
  border-bottom: none;
}

@media (hover: hover) {
  .daily-mover-row:hover {
    background: #f8f9fb;
  }
}

.daily-mover-row :deep(.symbol-logo) {
  width: 30px;
  height: 30px;
}

.daily-mover-row :deep(.symbol-cell) {
  min-width: 0;
}

.daily-mover-row :deep(.symbol-meta) {
  min-width: 0;
}

.daily-mover-row :deep(.symbol-meta .name) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.daily-mover-overview {
  min-width: 0;
}

.daily-mover-track {
  height: 8px;
  overflow: hidden;
  background: #e9edf3;
  border-radius: 999px;
}

.daily-mover-fill {
  display: block;
  height: 100%;
  border-radius: inherit;
}

.daily-mover-fill.up {
  background: linear-gradient(90deg, #20b49d, #089981);
}

.daily-mover-fill.down {
  background: linear-gradient(90deg, #ff6d78, #f23645);
}

.daily-mover-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  margin-top: 7px;
  overflow: hidden;
  color: #8a8f9b;
  font-size: 10px;
  white-space: nowrap;
}

.daily-mover-meta > span {
  flex: none;
}

.daily-mover-meta :deep(.ant-tag) {
  height: 18px;
  margin: 0;
  padding: 0 5px;
  font-size: 9px;
  line-height: 16px;
}

.daily-mover-value {
  min-width: 0;
  text-align: right;
}

.daily-mover-value strong,
.daily-mover-value span {
  display: block;
  font-variant-numeric: tabular-nums;
}

.daily-mover-value strong {
  overflow: hidden;
  color: #131722;
  font-size: 13px;
  font-weight: 750;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.daily-mover-value span {
  margin-top: 3px;
  font-size: 11px;
  font-weight: 650;
}

.daily-panel-empty {
  padding: 42px 10px;
  color: #9aa0aa;
  font-size: 12px;
  text-align: center;
}

.daily-mover-empty {
  padding: 14px 0 2px;
}

.daily-status--holding {
  color: #175cd3;
  background: #eff8ff;
  border-color: #c4e6ff;
}

.daily-status--intraday {
  color: #7a2e91;
  background: #fbf4ff;
  border-color: #ead0f4;
}

.daily-status--closed {
  color: #5f6676;
  background: #f5f6f8;
  border-color: #e1e5eb;
}

.muted-value {
  color: #b2b5be;
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

.quarter-panel {
  background: #fff;
  border: 1px solid #e0e3eb;
  border-radius: 8px;
  padding: 8px 12px 12px;
}

.quarter-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.year-cell {
  padding: 0;
  background: transparent;
  border: none;
  cursor: pointer;
  font: inherit;
  color: inherit;
  text-align: inherit;
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

@media (max-width: 800px) {
  .daily-holdings-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .daily-total {
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
  }

}

@media (max-width: 560px) {
  .daily-holdings-card {
    margin-top: 12px;
    padding: 12px;
    border-radius: 8px;
  }

  .daily-holdings-header {
    align-items: flex-start;
    flex-direction: column;
    gap: 10px;
  }

  .daily-total {
    grid-template-columns: 1fr;
    gap: 10px;
    width: 100%;
    box-sizing: border-box;
  }

  .daily-total-breakdown {
    justify-content: space-between;
    padding-top: 10px;
    padding-left: 0;
    border-top: 1px solid #e4e8ef;
    border-left: none;
  }

  .daily-mover-row {
    grid-template-columns: minmax(102px, 0.9fr) minmax(82px, 1fr) 82px;
    gap: 8px;
    min-height: 62px;
    padding: 8px 0;
  }

  .daily-mover-meta {
    gap: 4px;
  }

  .daily-mover-meta > span:nth-child(2) {
    display: none;
  }

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

  .quarter-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .year-cell .cell-body {
    min-height: 72px;
    border-radius: 0;
    border-left: none;
    border-top: none;
  }
}
</style>
