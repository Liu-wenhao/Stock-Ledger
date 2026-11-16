<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import dayjs from 'dayjs'
import * as echarts from 'echarts'
import {
  AimOutlined,
  ArrowRightOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CopyOutlined,
  LineChartOutlined,
  PieChartOutlined,
  ReloadOutlined,
  SafetyOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { fetchTradingStrategy, fetchStrategyEngine, fetchBacktest, fetchBacktestCandles, updateHoldingSleeve } from '@/api/portfolio'
import { useTheme } from '@/composables/useTheme'
import { formatMoney, formatPct, formatQty, pnlClass, tickerOf } from '@/utils/format'

const data = ref(null)
const loading = ref(true)
const error = ref('')
const range = ref([dayjs().subtract(1, 'month'), dayjs()])
const rangePresets = [
  { label: '近1个月', value: [dayjs().subtract(1, 'month'), dayjs()] },
  { label: '近3个月', value: [dayjs().subtract(3, 'month'), dayjs()] },
  { label: '今年', value: [dayjs().startOf('year'), dayjs()] },
]
const candidateFilter = ref('ready')
const backtest = ref(null)
const backtestLoading = ref(false)
const backtestError = ref('')
// 回测默认跑近一年，不跟随页面顶部的复盘日期。
const backtestRange = ref([dayjs().subtract(1, 'year'), dayjs()])
const backtestPresets = [
  { label: '近1年', value: [dayjs().subtract(1, 'year'), dayjs()] },
  { label: '近6个月', value: [dayjs().subtract(6, 'month'), dayjs()] },
  ...rangePresets,
]

const capital = computed(() => data.value?.capital || {})
const allocation = computed(() => data.value?.allocation || null)
const equity = computed(() => Number(allocation.value?.equity || 0))
const singleCap = computed(() => Number(allocation.value?.single_cap || capital.value.deposits * 0.06 || 0))
const coreSingleCap = computed(() => Number(allocation.value?.core_single_cap || capital.value.deposits * 0.5 / 3 || 0))
const tranche = computed(() => Number(allocation.value?.tranche || 0))
function sleeveOf(key) {
  return allocation.value?.sleeves?.find((item) => item.key === key) || {}
}
const behavior = computed(() => data.value?.behavior || {})
const openPositions = computed(() => data.value?.open_positions || [])
const candidates = computed(() => data.value?.reentry_candidates || [])
const exitPositions = computed(() => openPositions.value.filter((p) => p.action === 'exit'))
const readyCandidates = computed(() => candidates.value.filter((c) => c.status === 'ready'))
const coolingCandidates = computed(() => candidates.value.filter((c) => c.status === 'cooling'))
const watchingCandidates = computed(() =>
  candidates.value.filter((c) => !['ready', 'cooling'].includes(c.status)),
)
const positionValue = computed(() =>
  openPositions.value.reduce((sum, p) => sum + Number(p.market_value || 0), 0),
)
const positionPnl = computed(() =>
  openPositions.value.reduce((sum, p) => sum + Number(p.total_pnl ?? p.pnl ?? 0), 0),
)
const positionCost = computed(() =>
  openPositions.value.reduce((sum, p) => sum + Number(p.cost || 0), 0),
)
const positionPnlBase = computed(() =>
  openPositions.value.reduce(
    (sum, p) => sum + Number(p.cost || 0) - Number(p.realized_pnl || 0),
    0,
  ),
)
const positionPnlPct = computed(() =>
  positionPnlBase.value > 0 ? (positionPnl.value / positionPnlBase.value) * 100 : 0,
)
const candidateFilters = computed(() => [
  { value: 'ready', label: '条件满足', count: readyCandidates.value.length },
  { value: 'cooling', label: '冷却中', count: coolingCandidates.value.length },
  { value: 'watching', label: '等待信号', count: watchingCandidates.value.length },
  { value: 'all', label: '全部候选', count: candidates.value.length },
])
const filteredCandidates = computed(() => {
  if (candidateFilter.value === 'all') return candidates.value
  if (candidateFilter.value === 'watching') return watchingCandidates.value
  return candidates.value.filter((c) => c.status === candidateFilter.value)
})
function rangeParams() {
  const [start, end] = range.value || []
  if (!start || !end) return {}
  return { start: start.format('YYYY-MM-DD'), end: end.format('YYYY-MM-DD') }
}

// ---- strategy digest ----------------------------------------------------
const strategyRules = computed(() => data.value?.rules || [])
const rulesExpanded = ref(false)
const digestCopyOpen = ref(false)
const digestCopyText = ref('')
const digestCopyArea = ref(null)
const engineCode = ref('')
const copyingStrategy = ref(false)
const klineChecksAll = ref('all')
watch(klineChecksAll, () => {
  if (klineOpen.value && klineChart) renderKlineChart()
})
function selectDigestText() {
  const area = digestCopyArea.value
  if (!area) return
  area.focus()
  area.select()
  area.setSelectionRange(0, area.value.length)
}
watch(digestCopyOpen, (open) => {
  if (open) nextTick(selectDigestText)
})
const overCapNames = computed(() =>
  (allocation.value?.names || []).filter((row) => Number(row.room) < 0),
)
const timeStopPositions = computed(() =>
  openPositions.value.filter((p) => p.time_stop_due && p.action !== 'exit'),
)
const digestRange = computed(() => {
  const meta = data.value?.meta
  if (!meta?.start_date || !meta?.end_date) return '全部区间'
  return `${dateText(meta.start_date)} – ${dateText(meta.end_date)}`
})
const digestStamp = ref('')

const digestActions = computed(() => {
  const rows = []
  if (exitPositions.value.length) {
    rows.push({
      key: 'exit',
      tone: 'danger',
      count: exitPositions.value.length,
      title: '触发止损，今天清仓',
      detail: exitPositions.value.map((p) => tickerOf(p.symbol)).join('、'),
      anchor: '#strategy-positions',
      cta: '看持仓动作',
    })
  }
  if (timeStopPositions.value.length) {
    rows.push({
      key: 'time',
      tone: 'warn',
      count: timeStopPositions.value.length,
      title: '持有满 40 天，复核逻辑',
      detail: timeStopPositions.value.map((p) => tickerOf(p.symbol)).join('、'),
      anchor: '#strategy-positions',
      cta: '看持仓动作',
    })
  }
  if (overCapNames.value.length) {
    rows.push({
      key: 'cap',
      tone: 'warn',
      count: overCapNames.value.length,
      title: '超过单只上限',
      detail: overCapNames.value.map((row) => row.ticker).join('、'),
      anchor: '#strategy-allocation',
      cta: '看资金配置',
    })
  }
  if (readyCandidates.value.length) {
    rows.push({
      key: 'ready',
      tone: 'info',
      count: readyCandidates.value.length,
      title: '冷却已满且技术条件满足',
      detail: `${readyCandidates.value.map((c) => tickerOf(c.symbol)).join('、')} · 写下理由后只做 1/3 试仓`,
      anchor: '#strategy-reentry',
      cta: '看再买入',
    })
  }
  if (coolingCandidates.value.length) {
    rows.push({
      key: 'cooling',
      tone: 'muted',
      count: coolingCandidates.value.length,
      title: '冷却中，禁止买回',
      detail: coolingCandidates.value
        .map((c) => `${tickerOf(c.symbol)} 还差 ${c.cooldown_remaining} 日`)
        .join('、'),
      anchor: '#strategy-reentry',
      cta: '看再买入',
    })
  }
  if (!rows.length) {
    rows.push({
      key: 'clear',
      tone: 'good',
      count: 0,
      title: '今天没有必须执行的动作',
      detail: '持仓守止损，候选等信号；不为了操作而操作。',
      anchor: '#strategy-checklist',
      cta: '看下单清单',
    })
  }
  return rows
})

const scorecard = computed(() => {
  const b = behavior.value
  const num = (value) => {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : 0
  }
  const payoff = num(b.payoff)
  const winDays = num(b.avg_win_days)
  const lossDays = num(b.avg_loss_days)
  const addon = num(b.addon_below_avg)
  const flips = num(b.same_day_flips)
  const meter = (value, target) => Math.max(0, Math.min(100, (value / target) * 100))
  return [
    {
      key: 'payoff',
      label: '盈亏比',
      hint: '平均盈利 ÷ 平均亏损',
      value: payoff.toFixed(2),
      target: '≥ 1.50',
      pass: payoff >= 1.5,
      pct: meter(payoff, 1.5),
    },
    {
      key: 'hold',
      label: '赢家持有 ÷ 输家持有',
      hint: `${winDays.toFixed(1)} 天 对 ${lossDays.toFixed(1)} 天`,
      value: lossDays > 0 ? (winDays / lossDays).toFixed(2) : '—',
      target: '> 1.00',
      pass: lossDays > 0 && winDays > lossDays,
      pct: lossDays > 0 ? meter(winDays / lossDays, 1) : 0,
    },
    {
      key: 'addon',
      label: '逆势加仓次数',
      hint: '低于成本时补仓的笔数',
      value: String(addon),
      target: '= 0',
      pass: addon === 0,
      pct: addon === 0 ? 100 : 0,
    },
    {
      key: 'flip',
      label: '同日反手次数',
      hint: '同一天对同一标的一买一卖',
      value: String(flips),
      target: '= 0',
      pass: flips === 0,
      pct: flips === 0 ? 100 : 0,
    },
  ]
})

const scorecardPassed = computed(() => scorecard.value.filter((row) => row.pass).length)

const digestStatement = computed(() => {
  const core = Number(sleeveOf('core').target_pct || 50)
  const sat = Number(sleeveOf('satellite').target_pct || 30)
  const cash = Number(sleeveOf('cash').target_pct || 20)
  return `${formatMoney(equity.value, 'USD', 0)} 里核心最多 ${core}%、卫星最多 ${sat}%、现金不低于 ${cash}%；核心最多 3 只、卫星最多 5 只，各自按实际只数平分该类型的总额：现在核心单只不超过 ${formatMoney(coreSingleCap.value, 'USD', 0)}，卫星单只不超过 ${formatMoney(singleCap.value, 'USD', 0)}。趋势规则分三批每批 ${formatMoney(tranche.value, 'USD', 0)}，反转规则一次买入、单笔止损亏损不超过权益的 1%（${formatMoney(equity.value * 0.01, 'USD', 0)}）。四条买入规则同日共振时按「收缩突破 > 均线交叉 > RSI超卖 > 超跌反弹」取主导规则。1R 是该规则的 ATR 止损距离，止损锚定首笔入场价；吊灯与触及 MA20 止盈全天候生效，趋势规则再叠加 +1.5R / +2.5R 各减 1/3、尾仓跟随吊灯。收盘满足信号当天就按收盘价买卖；加仓要比上一批再抬升 0.25 倍 ATR；清仓后冷却 5 个交易日。`
})

function strategyText() {
  const lines = [
    '# 交易策略',
    '',
    '## 一、仓位与风险',
    '- 资金分层：核心 0～50%（最多 3 只）／卫星 0～30%（最多 5 只）／现金不低于 20%。每只持仓手动标为核心或卫星。没有信号就空着，不为凑比例买入。',
    '- 单只上限：该类型的总额按实际只数平分。核心 3 只时每只是权益的 16.7%，卫星 4 只时每只 7.5%、5 只时每只 6%。',
    '- 趋势规则分三批建仓，每批 = 单只上限 ÷ 3；反转规则一次买入，股数按「打到 ATR 止损时亏损不超过权益的 1%」反推，且不超过单只上限。',
    '- 1R = 该规则的止损 ATR 倍数 × 买入当日 ATR，即首笔入场价到 ATR 止损价的距离。',
    '- 下单前必须写出一句买入理由，写不出来就不买。',
    '',
    '## 二、买入规则（硬条件必须全中，辅助条件按门槛）',
    '同一天多条规则同时成立时，按「收缩突破 > 均线交叉 > RSI超卖 > 超跌反弹」取主导规则；建仓方式、止损、止盈与时间止损全部跟随主导规则，避免趋势仓被反转规则的触及 MA20 提前清掉。',
  ]
  for (const rule of strategyRules.value) {
    lines.push(
      '',
      `### ${rule.name}（${rule.kind === 'trend' ? '趋势 · 分三批' : '反转 · 一次买入'}）`,
      `建仓方式：${rule.sizing}`,
      `初始止损：${rule.stop_atr} 倍买入当日 ATR`,
      `硬条件 · ${rule.core.length} 项全中：`,
      ...rule.core.map((label, i) => `${i + 1}. ${label}`),
      `辅助条件 · ${rule.optional.length} 项中至少 ${rule.need} 项：`,
      ...rule.optional.map((label, i) => `${i + 1}. ${label}`),
      '离场：',
      ...(rule.exit_steps || []).map((step, i) => `${i + 1}. ${step}`),
    )
  }
  lines.push(
    '',
    '## 三、加仓与卖出纪律',
    '- 收盘满足买入或卖出信号，当天按收盘价成交。',
    '- 加仓只在盈利时发生，且要「再抬升一档」：收盘高于均价、并且比上一批买价高出至少 0.25 倍初始 ATR，当天按收盘价再买一批。最多三批。',
    '- 止损只有一套：各规则的 ATR 止损锚定首笔入场价，盈利加仓摊高均价后不会被正常回踩误杀。',
    '- 吊灯移动止盈与反转触及 MA20 止盈全天候生效，不必先完成分批减仓。',
    '- 只有趋势规则做分批减仓：+1.5R 减 1/3，+2.5R 再减 1/3，尾仓跟随 ATR 吊灯。反转规则不分批，直接按上面的止盈条件一次清仓。',
    '- 时间止损按规则区分：均线交叉满 15 日、收缩突破满 20 日，若浮盈仍不到 1.5 倍初始 ATR 就离场（浮盈达到 1.5 倍 ATR 自动豁免）；RSI超卖满 10 日、超跌反弹满 8 日无条件离场。',
    '- 全局失效兜底：持有满 40 天且四条买入规则都不成立则清仓。',
    '- 清仓后强制冷却 5 个交易日；重新建仓要写下新理由，并且只做 1/3 试仓。',
    '- 允许同日对同一标的买卖（做 T）；禁止多单与反向 ETF 并存。',
    '',
    '## 四、执行频率',
    '- 每天最多 5 笔，每周最多 12 笔；只在 22:30–23:10 挂限价单。',
    '- 单日亏损到账户 −2.5% 即停手到下一交易日。',
    '- 杠杆／反向 ETF 只做趋势，最短持有 3 天。',
    '',
    '## 五、核心引擎代码',
    '```python',
    engineCode.value.trimEnd(),
    '```',
  )
  return lines.join('\n')
}

async function loadEngineCode() {
  if (engineCode.value) return engineCode.value
  const result = await fetchStrategyEngine()
  engineCode.value = result?.engine_code || ''
  if (!engineCode.value) throw new Error('engine code empty')
  return engineCode.value
}

function legacyCopy(text) {
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.top = '-1000px'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  area.setSelectionRange(0, text.length)
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  document.body.removeChild(area)
  return ok
}

async function copyStrategy() {
  if (copyingStrategy.value) return
  copyingStrategy.value = true
  let text = ''
  try {
    await loadEngineCode()
    text = strategyText()
  } catch {
    copyingStrategy.value = false
    message.error('核心引擎代码没取到，请稍后重试')
    return
  }
  // The async clipboard API is denied in some embedded browsers; fall back to execCommand.
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      message.success('策略与核心引擎代码已复制')
      copyingStrategy.value = false
      return
    }
  } catch {
    /* fall through to the legacy path */
  }
  if (legacyCopy(text)) {
    message.success('策略与核心引擎代码已复制')
  } else {
    digestCopyText.value = text
    digestCopyOpen.value = true
  }
  copyingStrategy.value = false
}

// 核心、卫星是上限，现金是下限；gap 为负都表示越线。
function gapText(gap, key) {
  const amount = Number(gap)
  const floor = key === 'cash'
  if (!Number.isFinite(amount) || Math.abs(amount) < 1) return floor ? '贴着下限' : '贴着上限'
  const money = formatMoney(Math.abs(amount), 'USD', 0)
  if (floor) return amount > 0 ? `比下限多 ${money}` : `低于下限 ${money}`
  return amount > 0 ? `离上限还有 ${money}` : `超上限 ${money}`
}

function sleeveBound(sleeve) {
  return sleeve.key === 'cash' ? `不低于 ${sleeve.target_pct}%` : `0～${sleeve.target_pct}%`
}

function priceText(value) {
  return value == null ? '—' : formatMoney(value, 'USD', 2)
}

function dateText(value) {
  return value ? String(value).slice(0, 10).replaceAll('-', '.') : '—'
}

function candidateTone(candidate) {
  return candidate.status === 'ready'
    ? 'ready'
    : candidate.status === 'cooling'
      ? 'cooling'
      : candidate.status === 'unavailable'
        ? 'muted'
        : 'watching'
}

function stopHit(position, stopPrice) {
  const current = Number(position.current_price)
  const stop = Number(stopPrice)
  return Number.isFinite(current) && Number.isFinite(stop) && current <= stop
}

function mandatoryStopHit(position) {
  return stopHit(position, position.hard_stop_price)
}

function suggestionStopHit(position) {
  return !mandatoryStopHit(position) && stopHit(position, position.trailing_stop_price)
}

function sellTradeRoute(position) {
  return {
    path: '/trades',
    query: {
      new: '1',
      side: 'Sell',
      symbol: position.symbol,
      qty: String(Number(position.qty)),
      price: String(Number(position.current_price ?? position.avg_price)),
      notes: position.action === 'trim' ? '按策略时间止损复核后清仓' : '按策略止损清仓',
    },
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await fetchTradingStrategy(rangeParams())
    data.value = result
    digestStamp.value = dayjs().format('YYYY-MM-DD HH:mm')
    if (candidateFilter.value === 'ready' && !result.reentry_candidates?.some((c) => c.status === 'ready')) {
      candidateFilter.value = 'all'
    }
  } catch (e) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

const sleeveOptions = [
  { value: 'core', label: '核心' },
  { value: 'satellite', label: '卫星' },
]
const sleeveSaving = ref('')

async function changeSleeve(item, sleeve) {
  if (sleeve === item.sleeve_key) return
  sleeveSaving.value = item.symbol
  try {
    await updateHoldingSleeve({ symbol: item.symbol, sleeve })
    message.success(`${item.ticker} 已设为${sleeve === 'core' ? '核心' : '卫星'}`)
    await load()
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '没有保存')
  } finally {
    sleeveSaving.value = ''
  }
}

async function runBacktest() {
  backtestLoading.value = true
  backtestError.value = ''
  try {
    const [start, end] = backtestRange.value || []
    const params = {}
    if (start && end) {
      params.start = start.format('YYYY-MM-DD')
      params.end = end.format('YYYY-MM-DD')
    }
    backtest.value = await fetchBacktest(params)
  } catch (e) {
    backtestError.value = e?.message || '回测没有跑完'
  } finally {
    backtestLoading.value = false
  }
}

function signedMoney(value) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  const number = Number(value)
  const text = formatMoney(Math.abs(number), 'USD', 2)
  if (number > 0) return `+${text}`
  if (number < 0) return `-${text}`
  return text
}

function signedPct(value) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

function plainNumber(value) {
  return value == null || Number.isNaN(Number(value)) ? '—' : Number(value).toFixed(2)
}

function plainR(value) {
  return value == null || Number.isNaN(Number(value)) ? '—' : `${Number(value).toFixed(2)}R`
}

function signedR(value) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}R`
}

function clockText(value) {
  if (!value) return '—'
  const text = String(value)
  const parsed = /[zZ]|[+-]\d{2}:?\d{2}$/.test(text) ? dayjs(text) : dayjs(`${text.replace(' ', 'T')}Z`)
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm') : text
}

function triggerClock(value) {
  if (!value) return ''
  const text = String(value)
  const parsed = /[zZ]|[+-]\d{2}:?\d{2}$/.test(text) ? dayjs(text) : dayjs(`${text.replace(' ', 'T')}Z`)
  return parsed.isValid() ? parsed.format('MM-DD HH:mm') : text.slice(5, 16)
}

function bareMoney(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return formatMoney(value, 'USD', digits).replace(/ [A-Z]{3}$/, '')
}

const backtestCycles = computed(() => {
  const cycles = []
  for (const item of backtest.value?.items || []) {
    const events = []
    for (const trade of item.trades || []) {
      const qty = Number(trade.qty || 0)
      const priceIn = Number(trade.entry_price || 0)
      const priceOut = Number(trade.exit_price || 0)
      events.push({
        side: 'Buy',
        at: trade.entry_at || trade.entry,
        price: trade.entry_price,
        qty,
        total: qty * priceIn,
        commission: 0,
        rule: trade.rule || '',
        signal: trade.signal || '',
        signalRules: trade.signal_rules || [],
      })
      if (trade.exit_at || trade.exit) {
        events.push({
          side: 'Sell',
          at: trade.exit_at || trade.exit,
          price: trade.exit_price,
          qty,
          total: qty * priceOut,
          commission: Number(trade.commission || 0),
          pnl: Number(trade.pnl || 0),
          rule: trade.exit_rule || '',
        })
      }
    }
    events.sort((a, b) => String(a.at).localeCompare(String(b.at)) || (a.side === 'Buy' ? -1 : 1))
    let qty = 0
    let current = null
    for (const event of events) {
      if (qty <= 1e-6) {
        event.isEntry = event.side === 'Buy'
        current = {
          symbol: item.symbol,
          name: item.name,
          started_at: event.at,
          ended_at: '',
          triggerAt: event.side === 'Buy' ? event.at : '',
          triggerRule: event.side === 'Buy' ? event.rule || '' : '',
          fills: [],
          pnl: 0,
          rules: [],
        }
        cycles.push(current)
      }
      current.fills.push(event)
      if (event.rule && !current.rules.includes(event.rule)) current.rules.push(event.rule)
      if (event.side === 'Sell') current.pnl += Number(event.pnl || 0)
      qty += event.side === 'Buy' ? event.qty : -event.qty
      if (qty <= 1e-6) {
        qty = 0
        current.ended_at = event.at
        event.clear = true
      }
    }
  }
  return cycles
})

const tradeModalOpen = ref(false)
const tradeModalSymbol = ref('')
const tradeModalRows = ref([])

function openCycleTrades(cycle) {
  tradeModalSymbol.value = cycle.symbol
  tradeModalRows.value = [...cycle.fills].sort(
    (a, b) => String(b.at).localeCompare(String(a.at)) || (a.side === 'Sell' ? -1 : 1),
  )
  tradeModalOpen.value = true
}

function fillTitle(fill) {
  const base = `${fill.side === 'Buy' ? '买入' : '卖出'}  ${bareMoney(fill.price, 4)} × ${formatQty(fill.qty)}`
  return fill.clear ? `${base}  · 清仓` : base
}

function fillDesc(fill) {
  const parts = [clockText(fill.at), `总额 ${bareMoney(fill.total)}`]
  if (Number(fill.commission) > 0) parts.push(`手续费 ${bareMoney(fill.commission)}`)
  if (fill.rule) parts.push(fill.rule)
  if (fill.side === 'Buy' && fill.signal && fill.signal !== String(fill.at || '').slice(0, 10)) {
    parts.push(`信号 ${fill.signal.replaceAll('-', '.')} 收盘满足`)
  }
  if (Number(fill.count) > 1) parts.push(`${fill.count} 条记录合并为一笔`)
  return parts.join(' · ')
}

const { isDark } = useTheme()
const KLINE_BUY = '#2962ff'
const KLINE_SELL = '#f23645'
const KLINE_SIGNAL = '#f5a623'
const KLINE_MA = { ma5: '#8B93A3', ma10: '#7C5CBF', ma20: '#F07A1A' }

const klineOpen = ref(false)
const klineLoading = ref(false)
const klineError = ref('')
const klineCycle = ref(null)
const klineCandles = ref([])
const klineChartRef = ref(null)
let klineChart = null
let klineToken = 0
let klineRefreshing = false
const klineCache = new Map()

const klineTitle = computed(() => {
  const cycle = klineCycle.value
  if (!cycle) return '回测K线'
  return `${tickerOf(cycle.symbol)}${cycle.name ? ` · ${cycle.name}` : ''} 回测K线`
})

const klineFills = computed(() =>
  [...(klineCycle.value?.fills || [])].sort(
    (a, b) => String(a.at).localeCompare(String(b.at)) || (a.side === 'Buy' ? -1 : 1),
  ),
)

const klineCost = computed(() => {
  const buys = klineFills.value.filter((fill) => fill.side === 'Buy')
  const qty = buys.reduce((sum, fill) => sum + Number(fill.qty || 0), 0)
  if (qty <= 0) return null
  return buys.reduce((sum, fill) => sum + Number(fill.price || 0) * Number(fill.qty || 0), 0) / qty
})

const klineChipFills = computed(() => {
  const merged = []
  const index = new Map()
  for (const fill of klineFills.value) {
    const key = `${fill.side}|${fill.at}|${Number(fill.price || 0)}`
    const at = index.get(key)
    if (at == null) {
      index.set(key, merged.length)
      merged.push({
        ...fill,
        qty: Number(fill.qty || 0),
        total: Number(fill.total || 0),
        commission: Number(fill.commission || 0),
        pnl: Number(fill.pnl || 0),
        count: 1,
      })
    } else {
      const target = merged[at]
      target.qty += Number(fill.qty || 0)
      target.total += Number(fill.total || 0)
      target.commission += Number(fill.commission || 0)
      target.pnl += Number(fill.pnl || 0)
      target.count += 1
      target.clear = target.clear || fill.clear
    }
  }
  return merged
})

const klineExitNote = computed(() => {
  const splits = klineChipFills.value.filter((fill) => fill.side === 'Sell' && fill.count > 1)
  if (!splits.length) return ''
  const parts = splits.map(
    (fill) => `${dateText(fill.at)} 一次性卖出 ${formatQty(fill.qty)} 股（引擎拆成 ${fill.count} 条）`,
  )
  return `清仓其实是一笔卖出全部，不是分批卖。本轮分批建仓，回测引擎按每一批买入分别配对算盈亏，于是同一笔卖出被记成多条：${parts.join('；')}。图上已合并成一个 S 点，下方标签也并成一笔。`
})

function klinePalette() {
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

function sma(values, span) {
  const out = []
  let sum = 0
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i]
    if (i >= span) sum -= values[i - span]
    out.push(i >= span - 1 ? Number((sum / span).toFixed(4)) : null)
  }
  return out
}

function klineVolumeText(value) {
  const n = Number(value || 0)
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return String(Math.round(n))
}

function klineFillGroups(candles) {
  const dates = candles.map((bar) => bar.d)
  const groups = new Map()
  for (const fill of klineFills.value) {
    const day = String(fill.at || '').slice(0, 10)
    let idx = dates.indexOf(day)
    if (idx < 0) idx = dates.findIndex((d) => d >= day)
    if (idx < 0) idx = dates.length - 1
    if (idx < 0) continue
    const side = fill.side === 'Buy' ? 'B' : 'S'
    const key = `${idx}:${side}`
    if (!groups.has(key)) groups.set(key, { idx, side, fills: [] })
    groups.get(key).fills.push(fill)
  }
  return [...groups.values()].sort((a, b) => a.idx - b.idx || (a.side === 'B' ? -1 : 1))
}

async function loadKline(cycle, { fresh = false } = {}) {
  klineCycle.value = cycle
  klineError.value = ''
  klineCandles.value = []
  klineLoading.value = true
  const token = ++klineToken
  const start = String(cycle.started_at || '').slice(0, 10)
  const end = cycle.ended_at ? String(cycle.ended_at).slice(0, 10) : ''
  const key = `${cycle.symbol}|${start}|${end}`
  if (fresh) klineCache.delete(key)
  try {
    let payload = klineCache.get(key)
    if (!payload) {
      const params = {}
      if (start) params.start = start
      if (end) params.end = end
      payload = await fetchBacktestCandles(cycle.symbol, params)
      klineCache.set(key, payload)
    }
    if (token !== klineToken || !klineOpen.value) return
    klineCandles.value = payload.candles || []
    klineLoading.value = false
    await nextTick()
    renderKlineChart()
  } catch (e) {
    if (token !== klineToken) return
    klineLoading.value = false
    klineError.value = e?.response?.data?.detail || e?.message || 'K线没有取到'
  }
}

function openCycleKline(cycle) {
  klineOpen.value = true
  loadKline(cycle)
}

function retryKline() {
  if (klineCycle.value) loadKline(klineCycle.value, { fresh: true })
}

function disposeKlineChart() {
  klineRefreshing = false
  if (klineChart) {
    klineChart.dispose()
    klineChart = null
  }
}

function onKlineResize() {
  if (klineChart) klineChart.resize()
}

function buildKlineOption() {
  const candles = klineCandles.value
  const cycle = klineCycle.value
  const tone = klinePalette()
  const dates = candles.map((bar) => bar.d)
  const closes = candles.map((bar) => bar.c)
  const ma5 = sma(closes, 5)
  const ma10 = sma(closes, 10)
  const ma20 = sma(closes, 20)
  const up = '#089981'
  const down = '#f23645'
  const groups = klineFillGroups(candles)
  const fillsByDate = new Map()
  for (const group of groups) {
    const day = dates[group.idx]
    fillsByDate.set(day, [...(fillsByDate.get(day) || []), ...group.fills])
  }
  const tradeMarks = groups.map((group) => {
    const qty = group.fills.reduce((sum, fill) => sum + Number(fill.qty || 0), 0)
    const amount = group.fills.reduce((sum, fill) => sum + Number(fill.price || 0) * Number(fill.qty || 0), 0)
    const price = qty > 0 ? amount / qty : Number(group.fills[0]?.price || 0)
    return [group.idx, price]
  })
  const renderTradeMark = (params, api) => {
    const group = groups[params.dataIndex]
    if (!group) return null
    const buy = group.side === 'B'
    const color = buy ? KLINE_BUY : KLINE_SELL
    const center = api.coord([api.value(0), api.value(1)])
    const half = Math.max(api.size([1, 0])[0] / 2, 4)
    const cx = center[0]
    const cy = center[1]
    const guide = half * 0.94
    const stamp = triggerClock(group.fills[0]?.at)
    return {
      type: 'group',
      silent: true,
      children: [
        {
          type: 'line',
          shape: { x1: cx - guide, y1: cy, x2: cx + guide, y2: cy },
          style: {
            stroke: color,
            lineWidth: 2,
            lineCap: 'round',
            lineDash: buy ? undefined : [5, 3],
            opacity: 0.95,
          },
        },
        {
          type: 'circle',
          shape: { cx, cy, r: 6.5 },
          style: {
            fill: color,
            stroke: tone.paper,
            lineWidth: 2,
            shadowBlur: 6,
            shadowColor: buy ? 'rgba(41, 98, 255, 0.55)' : 'rgba(242, 54, 69, 0.55)',
          },
        },
        {
          type: 'text',
          style: {
            text: group.side,
            x: cx,
            y: cy,
            fill: '#fff',
            fontSize: 9,
            fontWeight: 700,
            align: 'center',
            verticalAlign: 'middle',
          },
        },
        ...(stamp
          ? [
              {
                type: 'text',
                style: {
                  text: stamp,
                  x: cx,
                  y: buy ? cy - 17 : cy + 17,
                  fill: color,
                  fontSize: 9,
                  fontWeight: 600,
                  align: 'center',
                  verticalAlign: 'middle',
                  backgroundColor: tone.paper,
                  borderColor: color,
                  borderWidth: 1,
                  borderRadius: 4,
                  padding: [2, 4],
                },
              },
            ]
          : []),
      ],
    }
  }
  const signalMarks = []
  const signalByDate = new Map()
  for (const fill of klineFills.value) {
    if (fill.side !== 'Buy') continue
    const sigDay = String(fill.signal || '').slice(0, 10)
    const execDay = String(fill.at || '').slice(0, 10)
    const sigIdx = dates.indexOf(sigDay)
    const execIdx = dates.indexOf(execDay)
    if (sigIdx < 0 || execIdx < 0) continue
    signalMarks.push({ value: [sigIdx, Number(fill.price || 0)], execIdx, fill })
    signalByDate.set(sigDay, [...(signalByDate.get(sigDay) || []), fill])
  }
  const renderSignalMark = (params, api) => {
    const mark = signalMarks[params.dataIndex]
    if (!mark) return null
    const fill = mark.fill
    const sigIdx = api.value(0)
    const sigBar = candles[sigIdx]
    const top = api.coord([sigIdx, sigBar.h])
    const sx = top[0]
    const my = top[1] - 16
    const exec = api.coord([mark.execIdx, api.value(1)])
    const label = fill.isEntry ? fill.rule || '信号' : '加仓'
    const bolt = [[2, -8], [-4, 2], [-1, 2], [-2, 8], [4, -2], [1, -2]].map(([px, py]) => [sx + px, my + py])
    return {
      type: 'group',
      silent: true,
      children: [
        {
          type: 'line',
          shape: { x1: sx, y1: my + 6, x2: exec[0], y2: exec[1] },
          style: { stroke: KLINE_SIGNAL, lineWidth: 1.2, lineDash: [3, 3], opacity: 0.65 },
        },
        {
          type: 'polygon',
          shape: { points: bolt },
          style: { fill: KLINE_SIGNAL, stroke: tone.paper, lineWidth: 1, shadowBlur: 6, shadowColor: 'rgba(245, 166, 35, 0.6)' },
        },
        {
          type: 'text',
          style: {
            text: label,
            x: sx,
            y: my - 13,
            fill: KLINE_SIGNAL,
            fontSize: 9,
            fontWeight: 650,
            align: 'center',
            verticalAlign: 'middle',
            backgroundColor: tone.paper,
            borderColor: KLINE_SIGNAL,
            borderWidth: 1,
            borderRadius: 4,
            padding: [1, 4],
          },
        },
      ],
    }
  }
  const catalog = strategyRules.value
  const condRow = (label, ok) =>
    `<div style="font-size:11px;line-height:1.6"><span style="color:${ok ? up : down};font-weight:700">${ok ? '✓' : '✗'}</span> ${label}</div>`
  const groupLabel = (text) =>
    `<div style="margin-top:4px;font-size:10px;font-weight:700;letter-spacing:.03em;color:${tone.axis}">${text}</div>`
  const ruleBlock = (name, badge, coreLabels, coreOk, optLabels, optOk, need) => {
    const coreHit = coreOk.filter(Boolean).length
    const optHit = optOk.filter(Boolean).length
    let h = `<div style="margin-top:5px;font-weight:650;color:${tone.ink}">${name} <span style="color:${tone.axis};font-weight:400;font-size:10px">${badge || `必填 ${coreHit}/${coreLabels.length} · 辅助 ${optHit}/${optLabels.length}（需 ${need}）`}</span></div>`
    h += groupLabel('必填：')
    h += coreLabels.map((label, k) => condRow(label, coreOk[k])).join('')
    h += groupLabel(`辅助：至少 ${need} 项`)
    h += optLabels.map((label, k) => condRow(label, optOk[k])).join('')
    return h
  }
  const signalLogicHtml = (fill) => {
    let h = `<div style="margin-top:6px;padding-top:6px;border-top:1px dashed ${tone.line}">`
    h += `<div style="color:${KLINE_SIGNAL};font-weight:650">⚡ 收盘触发信号 → 当天按收盘价执行</div>`
    if (!fill.isEntry) {
      h += `<div style="font-size:11px;color:${tone.ink};margin-top:2px">加仓信号：收盘高于均价、且比上一批买价再高 0.25 倍初始 ATR，当天按收盘价成交。</div>`
    } else if (fill.signalRules && fill.signalRules.length) {
      for (const rule of fill.signalRules) {
        h += ruleBlock(
          rule.name,
          `必填全中 · 辅助 ${rule.opt_hits}/${rule.need}`,
          (rule.core || []).map((c) => c.label),
          (rule.core || []).map((c) => c.ok),
          (rule.optional || []).map((c) => c.label),
          (rule.optional || []).map((c) => c.ok),
          rule.need,
        )
      }
    } else {
      h += `<div style="font-size:11px;color:${tone.ink};margin-top:2px">${fill.rule || '规则'}：必填条件全中。</div>`
    }
    h += '</div>'
    return h
  }
  const dayChecksHtml = (bar) => {
    const checks = bar.checks || []
    if (!checks.length || !catalog.length) return ''
    let h = `<div style="margin-top:6px;padding-top:6px;border-top:1px dashed ${tone.line}">`
    h += `<div style="font-weight:650;color:${tone.ink}">当日规则条件<span style="color:${tone.axis};font-weight:400;font-size:10px"> · 未触发买入</span></div>`
    let shown = 0
    checks.forEach((check, k) => {
      const rule = catalog[k]
      if (!rule) return
      if (!(check.core || []).some(Boolean)) return
      if (klineChecksAll.value === 'ready' && !check.ready) return
      shown += 1
      h += ruleBlock(
        rule.name,
        check.ready ? '✅ 全部满足' : '',
        rule.core,
        check.core || [],
        rule.optional,
        check.opt || [],
        rule.need,
      )
    })
    if (!shown) {
      h += `<div style="font-size:11px;color:${tone.axis};margin-top:3px">这一天没有规则命中必填条件。</div>`
    }
    h += '</div>'
    return h
  }
  const sellChecksHtml = (bar) => {
    // The server sends only the sell rules with at least one core hit.
    const sells = (bar.sells || []).filter((rule) => klineChecksAll.value !== 'ready' || rule.ready)
    if (!sells.length) return ''
    let h = `<div style="margin-top:6px;padding-top:6px;border-top:1px dashed ${tone.line}">`
    h += `<div style="font-weight:650;color:${tone.ink}">卖出规则条件</div>`
    for (const rule of sells) {
      h += ruleBlock(
        rule.name,
        rule.ready ? '全部满足' : '',
        (rule.core || []).map((c) => c.label),
        (rule.core || []).map((c) => c.ok),
        (rule.optional || []).map((c) => c.label),
        (rule.optional || []).map((c) => c.ok),
        rule.need,
      )
    }
    h += '</div>'
    return h
  }
  const firstIdx = groups.length ? groups[0].idx : 0
  const lastIdx = groups.length ? groups[groups.length - 1].idx : dates.length - 1
  const cost = klineCost.value
  const wash = Number(cycle?.pnl || 0) >= 0 ? 'rgba(8,153,129,0.07)' : 'rgba(242,54,69,0.06)'
  const zoomStart = dates[Math.max(0, firstIdx - 8)]
  const zoomEnd = dates[Math.min(dates.length - 1, lastIdx + 6)]
  const priceSpan = priceWindow(
    candles.map((bar) => bar.h),
    candles.map((bar) => bar.l),
    Math.max(0, dates.indexOf(zoomStart)),
    Math.max(0, dates.indexOf(zoomEnd)),
  )

  let lastTip = ''
  const tooltipFormatter = (params) => {
    const list = Array.isArray(params) ? params : [params]
    const hit = list.find((item) => Number.isInteger(item?.dataIndex))
    const i = hit?.dataIndex
    const bar = candles[i]
    if (bar == null) return lastTip
    const prev = i > 0 ? candles[i - 1].c : bar.o
    const chg = prev > 0 ? ((bar.c - prev) / prev) * 100 : 0
    const chgColor = chg >= 0 ? up : down
    const maRow = (label, value, color) =>
      value == null
        ? ''
        : `<span style="margin-right:10px"><i style="display:inline-block;width:8px;height:2px;background:${color};margin-right:4px;vertical-align:middle"></i>${label} ${bareMoney(value, 2)}</span>`
    let html = `<div style="font-size:12px;line-height:1.75;max-width:100%;overflow-wrap:anywhere;color:${tone.ink}">`
    html += `<div style="font-weight:650;margin-bottom:2px">${bar.d}</div>`
    html += `<div>开 ${bareMoney(bar.o, 2)} · 高 ${bareMoney(bar.h, 2)} · 低 ${bareMoney(bar.l, 2)}</div>`
    html += `<div>收 <b style="color:${chgColor}">${bareMoney(bar.c, 2)}</b> <span style="color:${chgColor}">${formatPct(chg)}</span> · 量 ${klineVolumeText(bar.v)}</div>`
    html += `<div style="margin-top:2px">${maRow('MA5', ma5[i], KLINE_MA.ma5)}${maRow('MA10', ma10[i], KLINE_MA.ma10)}${maRow('MA20', ma20[i], KLINE_MA.ma20)}</div>`
    const dayFills = fillsByDate.get(bar.d) || []
    for (const fill of dayFills) {
      const buy = fill.side === 'Buy'
      const color = buy ? KLINE_BUY : KLINE_SELL
      const execDay = String(fill.at || '').slice(0, 10)
      const signalNote = buy && fill.signal && fill.signal !== execDay
        ? `<div style="color:${tone.axis};font-size:10px;opacity:.85">信号 ${fill.signal.replaceAll('-', '.')} 收盘满足条件 → 下一交易日开盘执行</div>`
        : ''
      const exitNote = !buy && fill.rule
        ? `<div style="margin-top:3px;font-size:11px"><span style="color:${tone.axis};font-weight:700">卖出逻辑：</span><b style="color:${KLINE_SELL}">${fill.rule}</b></div>`
        : ''
      html += `<div style="margin-top:5px;padding-top:5px;border-top:1px dashed ${tone.line}"><b style="color:${color}">${buy ? 'B 买入' : 'S 卖出'}</b> ${bareMoney(fill.price, 4)} × ${formatQty(fill.qty)}<div style="color:${tone.axis};font-size:11px">成交 ${clockText(fill.at)}${buy && fill.rule ? ` · ${fill.rule}` : ''}</div>${exitNote}${signalNote}</div>`
    }
    const signalFills = signalByDate.get(bar.d) || []
    for (const fill of signalFills) {
      html += signalLogicHtml(fill)
    }
    if (!signalFills.length && !dayFills.some((fill) => fill.side === 'Sell')) {
      html += dayChecksHtml(bar)
    }
    html += sellChecksHtml(bar)
    html += '</div>'
    lastTip = html
    return html
  }

  const maSeries = (name, data, color, width) => ({
    name,
    type: 'line',
    showSymbol: false,
    smooth: 0.12,
    data,
    lineStyle: { width, color, cap: 'round', join: 'round' },
    itemStyle: { color },
  })

  return {
    animationDuration: 260,
    backgroundColor: 'transparent',
    legend: {
      data: ['K线', 'MA5', 'MA10', 'MA20', '触发信号'],
      top: 0,
      left: 0,
      itemWidth: 14,
      itemHeight: 3,
      icon: 'roundRect',
      textStyle: { color: tone.axis, fontSize: 11 },
      inactiveColor: tone.line,
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      trigger: 'axis',
      // Fixed band from the candle-grid bottom down to just above the zoom slider.
      // ECharts rewrites the tooltip style on every move, so the size is applied
      // again each time.
      confine: false,
      enterable: true,
      hideDelay: 400,
      transitionDuration: 0,
      position(_point, _params, dom, _rect, size) {
        const [viewWidth, viewHeight] = size.viewSize
        const top = Math.round(44 + viewHeight * 0.48) + 2
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
      axisPointer: { type: 'cross', lineStyle: { color: tone.line, type: 'dashed', width: 1 } },
      backgroundColor: tone.paper,
      borderColor: tone.line,
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: tone.ink },
      extraCssText: 'box-shadow:0 8px 24px rgba(20,30,55,.12);border-radius:10px;overflow-wrap:anywhere;',
      formatter: tooltipFormatter,
    },
    grid: [
      { left: 0, right: 0, top: 44, height: '48%' },
      { left: 0, right: 0, top: '68%', height: '16%' },
    ],
    xAxis: [0, 1].map((gridIndex) => ({
      type: 'category',
      gridIndex,
      data: dates,
      boundaryGap: true,
      axisLine: { lineStyle: { color: tone.line } },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: {
        show: gridIndex === 1,
        color: tone.axis,
        fontSize: 11,
        hideOverlap: true,
        formatter: (value) => String(value).slice(5),
      },
      axisPointer: { label: { show: false } },
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
      {
        gridIndex: 1,
        scale: true,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { show: false },
        axisPointer: { label: { show: false } },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], startValue: zoomStart, endValue: zoomEnd },
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        height: 18,
        bottom: 2,
        startValue: zoomStart,
        endValue: zoomEnd,
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
      },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: candles.map((bar) => [bar.o, bar.c, bar.l, bar.h]),
        itemStyle: { color: up, color0: down, borderColor: up, borderColor0: down },
        markArea: {
          silent: true,
          itemStyle: { color: wash },
          data: [[{ xAxis: dates[firstIdx] }, { xAxis: dates[lastIdx] }]],
        },
        markLine:
          cost == null
            ? undefined
            : {
                silent: true,
                symbol: 'none',
                lineStyle: { color: tone.axis, type: 'dashed', width: 1 },
                label: {
                  formatter: `成本 ${bareMoney(cost, 2)}`,
                  position: 'insideEndTop',
                  color: tone.axis,
                  fontSize: 10,
                },
                data: [{ yAxis: cost }],
              },
      },
      maSeries('MA5', ma5, KLINE_MA.ma5, 1.2),
      maSeries('MA10', ma10, KLINE_MA.ma10, 1.2),
      maSeries('MA20', ma20, KLINE_MA.ma20, 1.8),
      {
        name: '买卖点',
        type: 'custom',
        xAxisIndex: 0,
        yAxisIndex: 0,
        coordinateSystem: 'cartesian2d',
        renderItem: renderTradeMark,
        data: tradeMarks,
        silent: true,
        clip: true,
        z: 12,
        tooltip: { show: false },
      },
      {
        name: '触发信号',
        type: 'custom',
        xAxisIndex: 0,
        yAxisIndex: 0,
        coordinateSystem: 'cartesian2d',
        renderItem: renderSignalMark,
        data: signalMarks.map((mark) => mark.value),
        silent: true,
        clip: false,
        z: 11,
        itemStyle: { color: KLINE_SIGNAL },
        tooltip: { show: false },
      },
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: candles.map((bar) => ({
          value: bar.v,
          itemStyle: { color: bar.c >= bar.o ? 'rgba(8,153,129,0.55)' : 'rgba(242,54,69,0.55)' },
        })),
      },
    ],
  }
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

function syncKlinePriceTags() {
  if (!klineChart || klineRefreshing) return
  const bars = klineCandles.value
  if (!bars.length) return
  const option = klineChart.getOption()
  const categories = option.xAxis?.[0]?.data || bars.map((bar) => bar.d)
  const [start, end] = zoomIndexes(option.dataZoom?.[0], categories)
  const span = priceWindow(
    bars.map((bar) => bar.h),
    bars.map((bar) => bar.l),
    start,
    end,
  )
  if (span.hi == null) return
  klineRefreshing = true
  klineChart.setOption({ yAxis: [{ min: span.min, max: span.max }] })
  klineRefreshing = false
  requestAnimationFrame(() => {
    if (!klineChart) return
    placePriceTags(klineChart, span.hi, span.lo, (value) => bareMoney(value, 2), klinePalette().axis)
  })
}

function onKlineDataZoom() {
  syncKlinePriceTags()
}

function renderKlineChart() {
  if (!klineChartRef.value || !klineCandles.value.length || !klineCycle.value) return
  if (!klineChart) {
    klineChart = echarts.init(klineChartRef.value)
    klineChart.on('dataZoom', onKlineDataZoom)
  }
  klineChart.setOption(buildKlineOption(), true)
  klineChart.resize()
  requestAnimationFrame(() => syncKlinePriceTags())
}

watch(klineOpen, (open) => {
  if (!open) {
    klineToken += 1
    disposeKlineChart()
  }
})
watch(isDark, () => {
  if (klineOpen.value && klineChart) renderKlineChart()
})

onMounted(() => {
  load()
  window.addEventListener('resize', onKlineResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', onKlineResize)
  disposeKlineChart()
})
</script>

<template>
  <div class="strategy-page">
    <header class="page-header">
      <div>
        <p class="page-eyebrow">决策系统 / 交易策略</p>
        <h1>交易策略<span class="title-dot"></span></h1>
        <p class="page-description">把复盘里的发现，转化成每次下单前都能执行的规则。</p>
      </div>
      <div class="header-actions">
        <router-link class="review-link" to="/review">查看复盘 <span>→</span></router-link>
        <a-range-picker
          v-model:value="range"
          class="period-range"
          :allow-clear="false"
          format="YYYY.MM.DD"
          :presets="rangePresets"
          @change="load"
        />
        <a-button class="refresh-button" :loading="loading" @click="load">
          <template #icon><ReloadOutlined /></template>
          刷新策略
        </a-button>
      </div>
    </header>

    <a-spin :spinning="loading">
      <a-alert v-if="error" type="error" :message="error" show-icon class="err" />
      <div v-if="loading && !data" class="loading-panel">
        <a-skeleton active :paragraph="{ rows: 6 }" />
      </div>

      <template v-if="data">
        <section class="strategy-hero">
          <div class="hero-copy">
            <p class="eyebrow"><span class="eyebrow-dot"></span>当前执行框架</p>
            <h2>先守住风险，<br /><em>再让收益生长。</em></h2>
            <p class="hero-description">
              按当前权益 {{ formatMoney(equity || capital.deposits, 'USD', 0) }} 分成核心、卫星与现金。
              核心最多 50%，卫星最多 30%，现金不低于 20%，金额跟着现在的持仓和现金走。
            </p>
            <div class="hero-meta">
              <span>{{ data.meta.trades }} 笔历史交易</span>
              <span>{{ data.meta.symbols }} 个交易标的</span>
              <span>{{ openPositions.length }} 个当前持仓</span>
            </div>
          </div>

          <div class="hero-metrics">
            <article class="hero-metric">
              <span class="metric-icon blue"><PieChartOutlined /></span>
              <div><p>资金结构上限</p><strong>50<small> / 30 / 20</small></strong><span>核心上限 / 卫星上限 / 现金下限</span></div>
            </article>
            <article class="hero-metric">
              <span class="metric-icon teal"><AimOutlined /></span>
              <div><p>单只仓位上限</p><strong>{{ formatMoney(singleCap, 'USD', 0) }}</strong><span>卫星 {{ allocation?.satellite_count ?? 0 }} 只平分 · 核心 {{ formatMoney(coreSingleCap, 'USD', 0) }}（{{ allocation?.core_count ?? 0 }} 只平分）</span></div>
            </article>
            <article class="hero-metric">
              <span class="metric-icon amber"><SafetyOutlined /></span>
              <div><p>初始止损</p><strong>ATR</strong><span>1R = 规则止损倍数 × 买入当日 ATR</span></div>
            </article>
            <article class="hero-metric">
              <span class="metric-icon violet"><AimOutlined /></span>
              <div><p>盈亏比目标</p><strong>{{ behavior.payoff ?? '—' }}<small> → 1.5</small></strong><span>平均盈利 ÷ 平均亏损</span></div>
            </article>
          </div>
        </section>

        <nav class="strategy-nav" aria-label="策略内容导航">
          <span class="nav-label">执行手册</span>
          <a href="#strategy-digest">策略汇总</a>
          <a href="#strategy-allocation">资金配置</a>
          <a href="#strategy-rules">交易纪律</a>
          <a href="#strategy-positions">持仓动作</a>
          <a href="#strategy-reentry">再买入观察</a>
          <a href="#strategy-checklist">下单清单</a>
          <a href="#strategy-backtest">历史回测</a>
          <router-link to="/review" class="nav-next">回看交易数据 <span>→</span></router-link>
        </nav>

        <section id="strategy-digest" class="digest" aria-label="当前交易策略汇总">
          <div class="digest-top">
            <div class="digest-title">
              <p class="digest-eyebrow"><span class="digest-pulse"></span>一页看懂现在执行的策略</p>
              <h2>策略汇总</h2>
              <p class="digest-meta">
                <span>数据区间 {{ digestRange }}</span>
                <i aria-hidden="true"></i>
                <span>{{ digestStamp || '加载中' }} 生成</span>
                <i aria-hidden="true"></i>
                <span>{{ strategyRules.length }} 条买入规则</span>
              </p>
            </div>
            <div class="digest-tools">
              <a-button class="digest-btn primary" :loading="copyingStrategy" @click="copyStrategy">
                <template #icon><CopyOutlined /></template>
                复制策略
              </a-button>
            </div>
          </div>

          <p class="digest-statement">{{ digestStatement }}</p>

          <div class="digest-params">
            <article class="dparam teal">
              <span class="dparam-icon"><PieChartOutlined /></span>
              <div><p>当前权益</p><strong>{{ formatMoney(equity, 'USD', 0) }}</strong><span>持仓 {{ formatMoney(allocation?.holdings_value, 'USD', 0) }} ＋ 现金 {{ formatMoney(allocation?.cash, 'USD', 0) }}</span></div>
            </article>
            <article class="dparam blue">
              <span class="dparam-icon"><AimOutlined /></span>
              <div><p>单只上限</p><strong>{{ formatMoney(singleCap, 'USD', 0) }}</strong><span>卫星 · 核心 {{ formatMoney(coreSingleCap, 'USD', 0) }}</span></div>
            </article>
            <article class="dparam violet">
              <span class="dparam-icon"><LineChartOutlined /></span>
              <div><p>每批金额</p><strong>{{ formatMoney(tranche, 'USD', 0) }}</strong><span>卫星单只上限的 1/3，分三批</span></div>
            </article>
            <article class="dparam amber">
              <span class="dparam-icon"><SafetyOutlined /></span>
              <div><p>单笔最大亏损</p><strong>2×ATR</strong><span>每批股数 × 2 倍建仓日 ATR</span></div>
            </article>
            <article class="dparam red">
              <span class="dparam-icon"><ClockCircleOutlined /></span>
              <div><p>频率与冷却</p><strong>5<small> 笔/日</small></strong><span>每周 12 笔 · 清仓后冷却 5 个交易日</span></div>
            </article>
          </div>

          <div class="digest-split">
            <div class="digest-block">
              <h3><span class="dblock-tag">结构</span>界限 → 现在</h3>
              <div class="digest-sleeves">
                <div v-for="sleeve in (allocation?.sleeves || [])" :key="sleeve.key" class="dsleeve" :class="sleeve.key">
                  <div class="dsleeve-head">
                    <b>{{ sleeve.label }}</b>
                    <span class="dsleeve-nums">{{ sleeveBound(sleeve) }} <em>→</em> {{ Number(sleeve.current_pct).toFixed(1) }}%</span>
                  </div>
                  <div class="dsleeve-track">
                    <div class="dsleeve-target" :style="{ width: `${Math.min(Number(sleeve.target_pct), 100)}%` }"></div>
                    <div class="dsleeve-now" :style="{ width: `${Math.min(Number(sleeve.current_pct), 100)}%` }"></div>
                  </div>
                  <div class="dsleeve-foot">
                    <span>{{ formatMoney(sleeve.current_amount, 'USD', 0) }} / {{ formatMoney(sleeve.target_amount, 'USD', 0) }}</span>
                    <em>{{ gapText(sleeve.gap, sleeve.key) }}</em>
                  </div>
                </div>
              </div>
              <p v-if="overCapNames.length" class="digest-warn">
                <SafetyOutlined />
                {{ overCapNames.map((row) => `${row.ticker} ${Number(row.weight).toFixed(1)}%`).join('、') }} 超过单只上限，先减到上限内再谈加仓。
              </p>
              <p v-else class="digest-ok"><CheckCircleOutlined /> 没有标的超过单只上限。</p>
            </div>

            <div class="digest-block">
              <h3><span class="dblock-tag">今天</span>该做什么<a class="dblock-count">{{ digestActions.reduce((sum, row) => sum + row.count, 0) }} 项</a></h3>
              <ul class="digest-actions">
                <li v-for="action in digestActions" :key="action.key" class="daction" :class="action.tone">
                  <span class="daction-count">{{ action.count || '✓' }}</span>
                  <div>
                    <b>{{ action.title }}</b>
                    <span>{{ action.detail }}</span>
                  </div>
                  <a :href="action.anchor" class="daction-go">{{ action.cta }} <ArrowRightOutlined /></a>
                </li>
              </ul>
            </div>
          </div>

          <div class="digest-block">
            <h3>
              <span class="dblock-tag">进场</span>四条买入规则
              <span class="dblock-note">硬条件全中、辅助按门槛；同日共振取「收缩突破 &gt; 均线交叉 &gt; RSI超卖 &gt; 超跌反弹」为主导</span>
              <a class="dblock-toggle" @click.prevent="rulesExpanded = !rulesExpanded">
                {{ rulesExpanded ? '收起条件' : '展开全部条件' }}
              </a>
            </h3>
            <div class="digest-rules">
              <article
                v-for="rule in strategyRules"
                :key="rule.id"
                class="drule"
                :class="[rule.kind, { open: rulesExpanded }]"
              >
                <div class="drule-head">
                  <b>{{ rule.name }}</b>
                  <span class="drule-kind">{{ rule.kind === 'trend' ? '趋势 · 分三批' : '反转 · 一次买入' }}</span>
                </div>
                <p class="drule-gate">
                  <em>硬</em>{{ rule.core.length }} 项全中
                  <i aria-hidden="true"></i>
                  <em>辅</em>{{ rule.optional.length }} 中 {{ rule.need }}
                  <i aria-hidden="true"></i>
                  <em>止损</em>{{ rule.stop_atr }}×ATR
                </p>
                <div class="drule-body">
                  <p class="drule-label">硬条件</p>
                  <ol class="drule-list core">
                    <li v-for="(label, i) in rule.core" :key="`c-${i}`">{{ label }}</li>
                  </ol>
                  <p class="drule-label">辅助条件 · 至少 {{ rule.need }} 项</p>
                  <ol class="drule-list aux">
                    <li v-for="(label, i) in rule.optional" :key="`o-${i}`">{{ label }}</li>
                  </ol>
                  <div class="drule-exit">
                    <b>离场</b>
                    <ol class="drule-exit-list">
                      <li v-for="(step, i) in rule.exit_steps" :key="`e-${i}`">{{ step }}</li>
                    </ol>
                  </div>
                </div>
                <a class="drule-more" @click.prevent="rulesExpanded = !rulesExpanded">
                  {{ rulesExpanded ? '收起' : '看条件与离场' }}
                </a>
              </article>
            </div>
            <div class="digest-exit">
              <span class="dexit-title"><SafetyOutlined /> 卖出纪律 · 分批减仓只用于趋势规则</span>
              <div class="dexit-steps">
                <span class="dexit stop"><b>−1R</b><em>ATR 止损，整笔走</em></span>
                <ArrowRightOutlined class="dexit-arrow" />
                <span class="dexit take"><b>+1.5R</b><em>趋势减 1/3</em></span>
                <ArrowRightOutlined class="dexit-arrow" />
                <span class="dexit take"><b>+2.5R</b><em>趋势再减 1/3</em></span>
                <ArrowRightOutlined class="dexit-arrow" />
                <span class="dexit trail"><b>ATR 吊灯</b><em>清掉尾仓</em></span>
              </div>
              <p class="dexit-foot">各规则的 ATR 止损锚定首笔入场价；吊灯止盈与反转触及 MA20 全天候生效，不必先分批。时间止损：均线交叉 15 日、收缩突破 20 日（浮盈达 1.5 倍 ATR 豁免），RSI超卖 10 日、超跌反弹 8 日无条件；满 40 天四条规则都不成立则清仓。收盘满足信号当天按收盘价买卖；加仓要收盘高于均价且比上一批再高 0.25 倍 ATR；清仓后 5 个交易日不买回。</p>
            </div>
          </div>

          <div class="digest-block">
            <h3>
              <span class="dblock-tag">验收</span>策略做到了没有
              <span class="dblock-note">{{ behavior.closed_cycles || 0 }} 个已清仓周期 · 胜率 {{ Number(behavior.win_rate || 0).toFixed(0) }}%</span>
              <a class="dblock-count" :class="{ good: scorecardPassed === scorecard.length }">{{ scorecardPassed }}/{{ scorecard.length }} 达标</a>
            </h3>
            <div class="digest-score">
              <article v-for="row in scorecard" :key="row.key" class="dscore" :class="{ pass: row.pass }">
                <div class="dscore-head">
                  <b>{{ row.label }}</b>
                  <span class="dscore-flag">{{ row.pass ? '达标' : '未达标' }}</span>
                </div>
                <p class="dscore-value">{{ row.value }}<small>目标 {{ row.target }}</small></p>
                <div class="dscore-track"><div class="dscore-fill" :style="{ width: `${row.pct}%` }"></div></div>
                <span class="dscore-hint">{{ row.hint }}</span>
              </article>
            </div>
          </div>
        </section>

        <a-modal
          v-model:open="digestCopyOpen"
          title="手动复制策略"
          :footer="null"
          width="680px"
          wrap-class-name="mobile-sheet-modal digest-copy-modal"
          centered
        >
          <p class="digest-copy-hint">这个浏览器不允许网页直接写入剪贴板。下面是策略条文与核心引擎代码，已全选，按 Ctrl/Cmd + C 即可复制。</p>
          <textarea ref="digestCopyArea" class="digest-copy-area" readonly :value="digestCopyText" @focus="selectDigestText"></textarea>
          <div class="digest-copy-tools">
            <a-button class="digest-btn primary" @click="selectDigestText">
              <template #icon><CopyOutlined /></template>
              全选文本
            </a-button>
          </div>
        </a-modal>

        <h2 id="strategy-allocation" class="section-title"><span class="section-number">01</span>资金配置<span class="section-badge">按当前权益</span></h2>
        <p class="section-sub">核心最多 50%，卫星最多 30%，现金不低于 20%。下面用持仓市值加现金，看每一层离界限还有多远。</p>

        <div class="alloc-card">
          <div class="alloc-heading">
            <div>
              <p>当前权益</p>
              <span>{{ allocation?.note || '按持仓市值加现金计算。' }}</span>
            </div>
            <strong>{{ formatMoney(equity, 'USD', 0) }}</strong>
          </div>
          <p class="alloc-kicker">界限</p>
          <div class="alloc-bar" aria-label="核心最多 50%，卫星最多 30%，现金不低于 20%">
            <div class="seg core" style="flex: 50"><span>核心 ≤ 50%</span></div>
            <div class="seg sat" style="flex: 30"><span>卫星 ≤ 30%</span></div>
            <div class="seg cash" style="flex: 20"><span>现金 ≥ 20%</span></div>
          </div>
          <p class="alloc-kicker">现在</p>
          <div class="alloc-bar" aria-label="当前核心、卫星和现金占比">
            <div
              v-for="item in (allocation?.sleeves || []).filter((row) => Number(row.current_pct) >= 0.5)"
              :key="item.key"
              class="seg"
              :class="item.key === 'satellite' ? 'sat' : item.key"
              :style="{ flex: Math.max(Number(item.current_pct), 0) }"
            >
              <span v-if="Number(item.current_pct) >= 8">{{ item.label.replace('仓', '') }} {{ Number(item.current_pct).toFixed(0) }}%</span>
            </div>
          </div>
          <div class="alloc-list">
            <article v-for="item in allocation?.sleeves || []" :key="item.key" class="alloc-item" :class="item.key === 'satellite' ? 'sat' : item.key">
              <span class="alloc-index">{{ item.target_pct }}</span>
              <div>
                <p>{{ item.label }} <strong>{{ sleeveBound(item) }}</strong></p>
                <span>{{ item.key === 'cash' ? '下限' : '上限' }} {{ formatMoney(item.target_amount, 'USD', 0) }} · 现在 {{ formatMoney(item.current_amount, 'USD', 0) }}</span>
                <span :class="Number(item.gap) < -1 ? 'pnl-down' : ''">{{ gapText(item.gap, item.key) }}</span>
              </div>
            </article>
          </div>
          <div v-if="allocation?.names?.length" class="alloc-names">
            <div v-for="item in allocation.names" :key="item.symbol" class="alloc-name">
              <b>{{ item.ticker }}</b>
              <a-select
                :value="item.sleeve_key"
                :options="sleeveOptions"
                :loading="sleeveSaving === item.symbol"
                :disabled="!!sleeveSaving"
                size="small"
                class="alloc-sleeve"
                :aria-label="`${item.ticker} 的类型`"
                @change="(value) => changeSleeve(item, value)"
              />
              <span>{{ formatMoney(item.market_value, 'USD', 0) }}</span>
              <span>{{ Number(item.weight).toFixed(1) }}%</span>
              <span :class="Number(item.room) < 0 ? 'pnl-down' : ''">
                {{ Number(item.room) < 0 ? `超上限 ${formatMoney(Math.abs(item.room), 'USD', 0)}` : `距上限 ${formatMoney(item.room, 'USD', 0)}` }}
              </span>
            </div>
          </div>
        </div>

        <h2 id="strategy-rules" class="section-title"><span class="section-number">02</span>交易纪律<span class="section-badge">A–F 六条规则</span></h2>
        <p class="section-sub">每条规则都对应复盘中的具体问题，执行优先于解释。</p>

        <div class="strat-grid">
          <article class="strat">
            <span class="norm">A · 仓位</span>
            <h3>资金分层</h3>
            <ul>
              <li><b>核心 0～50%</b>：上限 {{ formatMoney(sleeveOf('core').target_amount, 'USD', 0) }}。最多 {{ allocation?.core_max ?? 3 }} 只，按实际只数平分；现在 {{ allocation?.core_count ?? 0 }} 只，单只不超过 {{ formatMoney(coreSingleCap, 'USD', 0) }}。</li>
              <li><b>卫星 0～30%</b>：上限 {{ formatMoney(sleeveOf('satellite').target_amount, 'USD', 0) }}。最多 {{ allocation?.satellite_max ?? 5 }} 只，按实际只数平分；现在 {{ allocation?.satellite_count ?? 0 }} 只，单只不超过 {{ formatMoney(singleCap, 'USD', 0) }}；单笔新建仓 {{ formatMoney(tranche, 'USD', 0) }}，分三批。</li>
              <li><b>现金不低于 20%</b>：下限 {{ formatMoney(sleeveOf('cash').target_amount, 'USD', 0) }}。到下限即停手，不允许卖出持仓来凑钱。</li>
            </ul>
            <p class="foot">单笔最大亏损 = 每批股数 × 2 倍建仓日 ATR，波动越大的票亏损上限越高。</p>
          </article>

          <article class="strat">
            <span class="norm">B · 建仓</span>
            <h3>只在盈利时加仓</h3>
            <ol>
              <li>下单前在备注写一句买入理由，写不出来就不买。</li>
              <li>分三批：1/3 试仓 → 站稳再加 → 突破再加。<b>只有趋势规则分批；收盘要高于均价，且比上一批买价再高出至少 0.25 倍初始 ATR，当天按收盘价再买一批。</b></li>
              <li>同一标的清仓后，<b>强制间隔 5 个交易日</b>才能重新建仓。</li>
            </ol>
            <div class="reentry-guide">
              <div class="reentry-head">
                <span class="reentry-icon"><ReloadOutlined /></span>
                <div>
                  <b>止损后再入</b>
                  <span>5 个交易日是最早时间，不是自动买回</span>
                </div>
              </div>
              <div class="reentry-flow" aria-label="止损后重新建仓的四项条件">
                <span>冷却 5 日</span><i>→</i><span>新理由</span><i>→</i><span>企稳 / 突破</span><i>→</i><span>1/3 试仓</span>
              </div>
              <div class="reentry-cases">
                <div class="fall">
                  <b>继续下跌</b>
                  <span>不补仓、不抄底；逻辑破坏就放弃。</span>
                </div>
                <div class="rise">
                  <b>直接反弹</b>
                  <span>不追高；满 5 日后等回踩或新突破。</span>
                </div>
              </div>
            </div>
            <p class="foot">第 6 个交易日不是买入信号；四项条件同时满足才重新建仓。</p>
          </article>

          <article class="strat">
            <span class="norm">C · 卖出</span>
            <h3>让止盈有尾巴</h3>
            <ol>
              <li>各规则的 <b>ATR 止损锚定首笔入场价</b>，盈利加仓摊高均价后不会被正常回踩误杀。</li>
              <li><b>趋势规则</b>才分批：1.5R 减 1/3，2.5R 再减 1/3，尾仓跟随 ATR 吊灯。<b>反转规则</b>一次清仓。</li>
              <li>吊灯止盈与反转触及 MA20 <b>全天候生效</b>，不必先完成分批减仓。</li>
              <li>时间止损：均线交叉 15 日、收缩突破 20 日（浮盈达 1.5 倍 ATR 豁免）；RSI超卖 10 日、超跌反弹 8 日无条件。</li>
              <li>持仓 <b>40 天</b>且四条规则都不成立，清掉。</li>
              <li>允许同日对同一标的买卖（做 T）；禁止多单与反向 ETF 并存。</li>
            </ol>
            <div class="risk-guide">
              <div class="risk-definition">
                <span class="risk-symbol">R</span>
                <div>
                  <b>初始风险单位</b>
                  <span>R = 该规则止损 ATR 倍数 × 买入当日 ATR</span>
                </div>
              </div>
              <div class="risk-levels" aria-label="止盈止损价格档位">
                <div class="risk-level stop">
                  <span>−1R</span>
                  <b>ATR 止损</b>
                  <small>按首笔价全部止损</small>
                </div>
                <div class="risk-level take">
                  <span>+1.5R</span>
                  <b>趋势</b>
                  <small>减仓 1/3</small>
                </div>
                <div class="risk-level take">
                  <span>+2.5R</span>
                  <b>趋势</b>
                  <small>再减 1/3</small>
                </div>
              </div>
              <div class="risk-follow">
                <span><b>尾仓</b><em>跟随 ATR 吊灯止盈</em></span>
                <span><b>时间止损</b><em>15/20 日浮盈不足 · 10/8 日无条件 · 40 日全局</em></span>
              </div>
              <p class="risk-example">
                R 跟着波动走，不是固定百分比。以 $100 入场、当日 ATR $3、均线交叉（2×ATR）为例：R = $6，止损锚定首笔价 $94；涨到 $109 减 1/3，$115 再减 1/3，尾仓在最高价回落 2.2 倍当日 ATR 时清掉。
              </p>
            </div>
            <p class="foot">针对“赢家跑得快、输家扛得久”。</p>
          </article>

          <article class="strat">
            <span class="norm">D · 频率</span>
            <h3>固定窗口下单</h3>
            <ul>
              <li>每天最多 <b>5 笔</b>，每周最多 <b>12 笔</b>。</li>
              <li>只在 <b>22:30–23:10</b> 挂限价单，之后关掉软件。冲动单写进“待观察”，次日再评。</li>
              <li>单日亏损到账户 <b>−2.5%</b> 即停手到下一交易日。</li>
              <li>杠杆 / 反向 ETF 只做趋势，最短持有 3 天。</li>
            </ul>
            <p class="foot">针对开盘后九十分钟的决策集中。</p>
          </article>

          <article class="strat">
            <span class="norm">E · 复盘</span>
            <h3>用数据校准</h3>
            <ul>
              <li><b>每周日</b>：标出逆势加仓与同日反手，不做交易决策。</li>
              <li><b>每月末</b>：算胜率、盈亏比、平均持仓天数，只看趋势。</li>
              <li><b>每季</b>：检查核心、卫星有没有超上限，现金有没有低于下限。</li>
            </ul>
            <p class="foot"><router-link to="/review">完整交易数据在复盘页查看 →</router-link></p>
          </article>

          <article class="strat">
            <span class="norm">F · 验收</span>
            <h3>一个月后看两个数</h3>
            <ul>
              <li><b>盈亏比 &gt; 1.5</b>：从现在的 {{ behavior.payoff }} 开始，先到 1.0，再到 1.5。</li>
              <li><b>平均持仓：赢 &gt; 输</b>：现在 {{ behavior.avg_win_days }} 对 {{ behavior.avg_loss_days }}。</li>
            </ul>
            <div class="payoff-guide">
              <div class="payoff-head">
                <b>盈亏比怎么算</b>
                <span>{{ behavior.closed_cycles }} 个已清仓周期</span>
              </div>
              <div class="payoff-equation" aria-label="平均盈利除以平均亏损等于盈亏比">
                <div><span>平均盈利</span><b>{{ formatMoney(behavior.avg_win, 'USD', 0) }}</b></div>
                <i>÷</i>
                <div><span>平均亏损</span><b>{{ formatMoney(behavior.avg_loss, 'USD', 0) }}</b></div>
                <i>=</i>
                <strong>{{ behavior.payoff }}</strong>
              </div>
              <p>按完整清仓周期计算，分批止盈归入同一周期；未扣手续费。当前 {{ behavior.wins }} 个盈利周期、{{ behavior.losses }} 个亏损周期，1.5 是目标值。</p>
            </div>
            <p class="foot">这两个数比收益率更早反映方法是否改变。</p>
          </article>
        </div>

        <h2 id="strategy-positions" class="section-title"><span class="section-number">03</span>当前持仓怎么处理<span class="section-badge">{{ openPositions.length }} 个持仓</span></h2>
        <p class="section-sub">行情和止损位由当前周期的收盘价计算；盈亏与持仓页一致，包含本周期分批卖出的已实现部分。红色动作为规则已触发，需要人工确认并记录卖出。</p>

        <div class="execution-overview">
          <div class="execution-stat">
            <span>持仓市值</span>
            <strong>{{ formatMoney(positionValue, 'USD', 2) }}</strong>
            <small>成本 {{ formatMoney(positionCost, 'USD', 2) }}</small>
          </div>
          <div class="execution-stat">
            <span>本周期累计盈亏</span>
            <strong :class="pnlClass(positionPnl)">{{ formatMoney(positionPnl, 'USD', 2) }}</strong>
            <small :class="pnlClass(positionPnl)">{{ formatPct(positionPnlPct) }}</small>
          </div>
          <div class="execution-stat alert">
            <span>止损待执行</span>
            <strong>{{ exitPositions.length }}<small> 个</small></strong>
            <small>{{ exitPositions.length ? '卖出全部剩余仓位' : '当前没有触发止损' }}</small>
          </div>
        </div>

        <div class="exit-playbook">
          <div class="playbook-heading">
            <div>
              <span class="playbook-kicker"><SafetyOutlined /> 清仓标准流程</span>
              <h3>止损触发后，先把这笔交易完整结束</h3>
            </div>
            <p>页面不会代替你下单。点击“记录清仓”会打开卖出表，并预填代码、数量和当前价格。</p>
          </div>
          <div class="workflow-steps">
            <div class="workflow-step">
              <i>01</i>
              <span><b>一次卖完</b><em>清掉全部剩余仓位，不留下“再等等”的尾仓。</em></span>
            </div>
            <ArrowRightOutlined class="workflow-arrow" />
            <div class="workflow-step">
              <i>02</i>
              <span><b>当天记账</b><em>保存卖出交易，让持仓、盈亏和清仓周期同步更新。</em></span>
            </div>
            <ArrowRightOutlined class="workflow-arrow" />
            <div class="workflow-step">
              <i>03</i>
              <span><b>冷却 5 日</b><em>从清仓后的下一交易日起算，期间不买回。</em></span>
            </div>
            <ArrowRightOutlined class="workflow-arrow" />
            <div class="workflow-step">
              <i>04</i>
              <span><b>重新审核</b><em>写明新理由，再等企稳或突破；只做 1/3 试仓。</em></span>
            </div>
          </div>
        </div>

        <div v-if="openPositions.length" class="position-grid">
          <article
            v-for="p in openPositions"
            :key="p.symbol"
            class="position-card"
            :class="[p.action, p.action_tone, { overdue: p.action === 'exit' && p.action_tone !== 'warning' }]"
          >
            <div class="position-card-head">
              <div class="symbol-name">
                <span class="ticker-avatar">{{ tickerOf(p.symbol).slice(0, 1) }}</span>
                <span class="position-ident">
                  <span class="sym">{{ tickerOf(p.symbol) }}</span>
                  <small>{{ Number(p.qty) }} 股 · {{ dateText(p.cycle_started_at) }} 建仓</small>
                </span>
              </div>
              <span class="execution-pill" :class="p.action_tone">{{ p.action_label }}</span>
            </div>

            <div class="position-numbers">
              <div>
                <span>当前价</span>
                <strong>{{ priceText(p.current_price) }}</strong>
              </div>
              <div>
                <span>持仓市值</span>
                <strong>{{ formatMoney(p.market_value, 'USD', 2) }}</strong>
              </div>
              <div>
                <span>本周期累计盈亏</span>
                <strong :class="pnlClass(p.total_pnl)">
                  {{ formatMoney(p.total_pnl, 'USD', 2) }}
                  <em>{{ formatPct(p.total_pnl_percent) }}</em>
                </strong>
              </div>
            </div>

            <div class="position-plan">
              <div :class="{ 'suggestion-stop': suggestionStopHit(p) }">
                <span>建议止损</span>
                <strong>{{ priceText(p.trailing_stop_price) }}</strong>
                <small v-if="p.trailing_stop_price == null">浮盈到过 2.5 倍 ATR 后启用</small>
                <small v-else>高点 {{ priceText(p.high_water_price) }} − 2.2 倍 ATR</small>
              </div>
              <div :class="{ 'mandatory-stop': mandatoryStopHit(p) }">
                <span>必须止损</span>
                <strong>{{ priceText(p.hard_stop_price) }}</strong>
                <small>成本 {{ priceText(p.avg_price) }} − 2 倍建仓日 ATR</small>
              </div>
              <div>
                <span>已持有</span>
                <strong>{{ p.holding_days }} 天</strong>
                <small :class="{ due: p.time_stop_due }">{{ p.time_stop_due ? '已到 40 天复核线' : '40 天触发时间复核' }}</small>
              </div>
            </div>

            <p class="position-action-note" :class="{ 'suggestion-exit-note': p.action_tone === 'warning' }">{{ p.action_note }}</p>
            <div class="position-card-foot">
              <span v-if="p.action === 'exit'">跌破建议止损即全部清仓；低于必须止损位（成本 − 2 倍建仓日 ATR）绝不能继续持有。</span>
              <span v-else-if="p.action === 'trim'">已到 40 天复核线：没有新逻辑就清仓；保留则不加仓。</span>
              <span v-else>建议止损还没启用时，按必须止损执行；建议止损只随新高上移。</span>
              <router-link v-if="['exit', 'trim'].includes(p.action)" :to="sellTradeRoute(p)" class="clear-action">
                {{ p.action === 'trim' ? '复核后清仓' : '记录清仓' }} <ArrowRightOutlined />
              </router-link>
              <router-link v-else to="/holdings" class="muted-action">查看持仓 →</router-link>
            </div>
          </article>
        </div>
        <div v-else class="empty-panel">
          <span class="empty-mark"><CheckCircleOutlined /></span>
          <div><b>当前没有持仓</b><p>下一笔买入仍要经过理由、止损和仓位检查。</p></div>
        </div>

        <h2 id="strategy-reentry" class="section-title"><span class="section-number">04</span>清仓后再买入<span class="section-badge">{{ readyCandidates.length }} 只条件满足</span></h2>
        <p class="section-sub">筛选所选区间里已经清仓、现在没有持仓的标的，优先显示满足企稳或突破条件的股票。这是待复核观察名单，不是自动买入。当前持仓的处理不随日期改变。</p>

        <div class="reentry-callout" :class="{ positive: readyCandidates.length }">
          <span class="callout-icon">
            <CheckCircleOutlined v-if="readyCandidates.length" />
            <ClockCircleOutlined v-else />
          </span>
          <div>
            <b>{{ readyCandidates.length ? `${readyCandidates.length} 只已通过技术筛选` : '今天没有新增技术买点' }}</b>
            <p>
              先确认已满 5 个交易日，再写下新的买入理由并人工复核。技术条件满足后只允许 1/3 试仓；
              继续下跌就放弃，直接反弹也不要追赶。
            </p>
          </div>
          <span class="callout-rule">5 日冷却 · 新理由 · 技术信号 · 1/3 试仓</span>
        </div>

        <div class="candidate-filter" role="tablist" aria-label="重新买入候选筛选">
          <button
            v-for="filter in candidateFilters"
            :key="filter.value"
            type="button"
            :class="{ active: candidateFilter === filter.value }"
            @click="candidateFilter = filter.value"
          >
            {{ filter.label }} <span>{{ filter.count }}</span>
          </button>
        </div>

        <div v-if="filteredCandidates.length" class="candidate-grid">
          <article
            v-for="c in filteredCandidates"
            :key="c.symbol"
            class="candidate-card"
            :class="candidateTone(c)"
          >
            <div class="candidate-head">
              <span class="symbol-name">
                <span class="ticker-avatar candidate-avatar">{{ tickerOf(c.symbol).slice(0, 1) }}</span>
                <span class="position-ident">
                  <span class="sym">{{ tickerOf(c.symbol) }}</span>
                  <small>{{ dateText(c.exited_at) }} 清仓</small>
                </span>
              </span>
              <span class="candidate-status" :class="candidateTone(c)">{{ c.status_label }}</span>
            </div>

            <div class="candidate-signal">
              <span>技术信号</span>
              <b>{{ c.technical_signal || '尚未触发' }}</b>
              <small>{{ c.sessions_estimated ? '约 ' : '' }}已过 {{ c.sessions_since_exit }} 个交易日</small>
            </div>

            <div class="candidate-metrics">
              <div>
                <span>当前价</span>
                <b>{{ priceText(c.current_price) }}</b>
                <em v-if="c.change_since_exit_pct != null" :class="pnlClass(c.change_since_exit_pct)">
                  {{ formatPct(c.change_since_exit_pct) }} 较清仓
                </em>
                <em v-else>—</em>
              </div>
              <div><span>上次清仓价</span><b>{{ priceText(c.exit_price) }}</b></div>
              <div><span>10 日均线</span><b>{{ priceText(c.ma10) }}</b></div>
              <div><span>前 20 日高点</span><b>{{ priceText(c.previous_20_high) }}</b></div>
            </div>

            <div class="cycle-result">
              <span>上次周期结果</span>
              <b :class="pnlClass(c.prior_realized)">
                {{ formatMoney(c.prior_realized, 'USD', 2) }}
                <em>{{ formatPct(c.prior_return_pct) }}</em>
              </b>
            </div>

            <p class="candidate-note">{{ c.status_note }}</p>

            <div v-if="c.status === 'ready'" class="manual-check ready">
              <CheckCircleOutlined />
              <span><b>下一步：人工确认</b>写下新买入理由，检查仓位不超过单只上限，再下 1/3 试仓单。</span>
            </div>
            <div v-else-if="c.status === 'cooling'" class="manual-check cooling">
              <ClockCircleOutlined />
              <span><b>还需冷却 {{ c.cooldown_remaining }} 个交易日</b>冷却期内即使反弹也不买回。</span>
            </div>
            <div v-else class="manual-check muted">
              <ClockCircleOutlined />
              <span><b>{{ c.status === 'unavailable' ? '行情暂不足' : '继续观察' }}</b>等价格站上 10 日均线且均线转升，或突破前 20 个收盘高点。</span>
            </div>
          </article>
        </div>
        <div v-else class="empty-panel candidate-empty">
          <span class="empty-mark"><ClockCircleOutlined /></span>
          <div>
            <b>这一类目前没有候选</b>
            <p v-if="candidateFilter === 'ready'">切到“全部候选”查看冷却中和等待信号的股票。</p>
            <p v-else>行情更新后，符合冷却和技术条件的标的会自动出现在这里。</p>
          </div>
        </div>

        <h2 id="strategy-checklist" class="section-title"><span class="section-number">05</span>每次下单前过一遍<span class="section-badge">检查清单</span></h2>
        <p class="section-sub">五秒钟能回答完，再打开交易软件。</p>
        <ol class="checklist">
          <li><span>这笔买入的理由能写成一句话吗？<b>写不出来就不买。</b></span></li>
          <li><span>当前价格<b>高于</b>持仓成本吗？低于的话，这个周期已经补过一次了吗？</span></li>
          <li><span>止损价是多少？<b>按 ATR 止损价算，亏多少美元，占账户多少？</b></span></li>
          <li><span>这个标的是不是已经有仓位，或持有反向 / 杠杆 ETF？</span></li>
          <li><span>今天下了几笔？现在是不是 22:30–23:10 之外？</span></li>
        </ol>

        <h2 id="strategy-backtest" class="section-title"><span class="section-number">06</span>历史回测<span class="section-badge">AKQuant</span></h2>
        <p class="section-sub">在选定的日期之间重跑，默认是近一年。清空日期则用全部已有日线。跑的是全部当前持仓，不限只数；持仓不足 4 只时，把观察名单里的标的全部加进来一起跑。总资金美股 3 万美元、A 股 30 万人民币：核心的标的平分 50%，卫星的标的平分 30%，其余留作现金；用观察名单补足时不分核心卫星，全部标的平分 80%。数字不进累计盈亏。</p>
        <div class="backtest-bar">
          <a-range-picker
            v-model:value="backtestRange"
            class="period-range backtest-range"
            format="YYYY.MM.DD"
            :presets="backtestPresets"
            :placeholder="['开始日期', '结束日期']"
          />
          <a-button class="refresh-button backtest-run" :loading="backtestLoading" @click="runBacktest">重跑一遍</a-button>
        </div>
        <a-alert v-if="backtestError" type="error" :message="backtestError" show-icon class="err" />
        <p v-if="backtest" class="section-sub">{{ backtest.profile }}。{{ backtest.note }}</p>
        <div v-if="backtest" class="bt-grid bt-result">
          <article class="hero-metric">
            <div>
              <p>本金</p>
              <strong>{{ formatMoney(backtest.cash, 'USD', 0) }}</strong>
              <span>{{ backtest.items?.length || 0 }} 只 · 现金留 {{ formatMoney(backtest.idle_cash, 'USD', 0) }}</span>
            </div>
          </article>
          <article class="hero-metric">
            <div>
              <p>期末</p>
              <strong>{{ backtest.end_value == null ? '—' : formatMoney(backtest.end_value, 'USD', 2) }}</strong>
              <span>含还没卖出的持仓</span>
            </div>
          </article>
          <article class="hero-metric">
            <div>
              <p>最终盈亏</p>
              <strong :class="pnlClass(backtest.pnl)">{{ signedMoney(backtest.pnl) }}</strong>
              <span>{{ signedPct(backtest.return_pct) }}</span>
            </div>
          </article>
        </div>
        <div v-if="backtest?.items?.length" class="bt-grid">
          <article v-for="item in backtest.items" :key="item.symbol" class="hero-metric">
            <span class="metric-icon teal"><AimOutlined /></span>
            <div>
              <p>{{ tickerOf(item.symbol) }}<template v-if="item.name"> · {{ item.name }}</template><template v-if="item.sleeve"> · {{ item.sleeve }} {{ formatMoney(item.cash, 'USD', 0) }}</template></p>
              <strong :class="pnlClass(item.pnl)">{{ item.end_value == null ? '—' : signedMoney(item.pnl) }}</strong>
              <span v-if="item.end_value == null">{{ item.error || '没有跑成' }}</span>
              <span v-else>{{ signedPct(item.return_pct) }} · {{ item.rule }} · 回撤 {{ item.max_drawdown_pct == null ? '—' : `${item.max_drawdown_pct.toFixed(1)}%` }} · 胜率 {{ item.win_rate == null ? '—' : `${item.win_rate.toFixed(0)}%` }}</span>
            </div>
          </article>
        </div>
        <template v-if="backtest?.compare?.length">
          <h3 class="bt-sub">和买入不动比</h3>
          <div class="table-wrap bt-cycles">
            <table>
              <thead>
                <tr>
                  <th>做法</th>
                  <th class="num">期末</th>
                  <th class="num">盈亏</th>
                  <th class="num">收益</th>
                  <th class="num">年化</th>
                  <th class="num">最大回撤</th>
                  <th class="num">夏普</th>
                  <th class="num">Calmar</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in backtest.compare" :key="row.label">
                  <td class="rule-cell bt-note"><b>{{ row.label }}</b><small>{{ row.note }}</small></td>
                  <td class="num">{{ row.end_value == null ? '—' : bareMoney(row.end_value) }}</td>
                  <td class="num" :class="pnlClass(row.pnl)">{{ signedMoney(row.pnl) }}</td>
                  <td class="num" :class="pnlClass(row.return_pct)">{{ signedPct(row.return_pct) }}</td>
                  <td class="num">{{ signedPct(row.annual_pct) }}</td>
                  <td class="num">{{ row.max_drawdown_pct == null ? '—' : `${row.max_drawdown_pct.toFixed(1)}%` }}</td>
                  <td class="num">{{ plainNumber(row.sharpe) }}</td>
                  <td class="num">{{ plainNumber(row.calmar) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="section-sub">三行用的是同一笔总资金、同一段日期，每天按收盘价算市值。夏普 = 日收益均值 ÷ 日收益波动 × √252，无风险利率按 0 算；Calmar = 年化收益 ÷ 最大回撤。留着不动的现金会同时压低收益和波动。</p>
        </template>
        <template v-if="backtest?.rule_stats?.length">
          <h3 class="bt-sub">按规则看</h3>
          <div class="table-wrap bt-cycles">
            <table>
              <thead>
                <tr>
                  <th>主导规则</th>
                  <th class="num">轮数</th>
                  <th class="num">胜率</th>
                  <th class="num">盈亏比</th>
                  <th class="num">平仓笔数</th>
                  <th class="num">平均每笔</th>
                  <th class="num">平均持有</th>
                  <th class="num">毛盈亏</th>
                  <th class="num">佣金税费</th>
                  <th class="num">净盈亏</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in backtest.rule_stats" :key="row.rule" :class="{ 'bt-total': row.rule === '全部' }">
                  <td class="sym">{{ row.rule }}<small v-if="row.cycles < 30" class="bt-thin">不足 30 轮</small></td>
                  <td class="num">{{ row.cycles }}</td>
                  <td class="num">{{ row.win_rate == null ? '—' : `${row.win_rate.toFixed(0)}%` }}</td>
                  <td class="num">{{ plainNumber(row.payoff) }}</td>
                  <td class="num">{{ row.lots }}</td>
                  <td class="num" :class="pnlClass(row.expect_r)">{{ signedR(row.expect_r) }}</td>
                  <td class="num">{{ row.held == null ? '—' : `${row.held.toFixed(1)} 日` }}</td>
                  <td class="num" :class="pnlClass(row.gross)">{{ signedMoney(row.gross) }}</td>
                  <td class="num">{{ bareMoney(row.commission) }}</td>
                  <td class="num" :class="pnlClass(row.net)">{{ signedMoney(row.net) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="section-sub">
            只算已经清仓的轮。一轮归给建仓当天的主导规则。一轮里每卖掉一批算一笔平仓。“平均每笔”是每一笔的净盈亏 ÷（该规则的 ATR 止损距离 × 这笔的股数）再取平均，单位是 R；每笔不论股数多少权重相同，所以它和净盈亏可能方向相反，两列要一起看。分批的规则在盈利的轮里拆出的笔数更多，这一列会偏高一些，最终以净盈亏为准。不足 30 轮的只作参考。
            <template v-if="backtest.cost">已平仓 {{ backtest.cost.trades }} 笔：毛盈亏 {{ signedMoney(backtest.cost.gross) }} − 佣金税费 {{ bareMoney(backtest.cost.commission) }} = 净 {{ signedMoney(backtest.cost.net) }}<template v-if="backtest.cost.share_pct != null">，佣金税费占毛盈亏 {{ backtest.cost.share_pct.toFixed(1) }}%</template>。滑点已经算在成交价里，不在佣金税费这一列。“最终盈亏”还包含没卖出的持仓，所以和这里的净盈亏不相等。</template>
          </p>
        </template>
        <template v-if="backtest?.excursions?.length">
          <h3 class="bt-sub">止损和止盈的距离够不够</h3>
          <div class="table-wrap bt-cycles">
            <table>
              <thead>
                <tr>
                  <th>主导规则</th>
                  <th class="num">止损</th>
                  <th class="num">轮数</th>
                  <th class="num">盈利轮</th>
                  <th class="num">盈利轮最深回撤<br />中位 / 75 分位 / 最大</th>
                  <th class="num">亏损轮最高浮盈<br />中位</th>
                  <th class="num">盘中碰到止损线<br />其中最后盈利</th>
                  <th class="num">浮盈到过<br />+1.5R / +2.5R</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in backtest.excursions" :key="row.rule" :class="{ 'bt-total': row.rule === '全部' }">
                  <td class="sym">{{ row.rule }}<small v-if="row.cycles < 30" class="bt-thin">不足 30 轮</small></td>
                  <td class="num">{{ row.stop_atr == null ? '—' : `${row.stop_atr}×ATR` }}</td>
                  <td class="num">{{ row.cycles }}</td>
                  <td class="num">{{ row.wins }}</td>
                  <td class="num">{{ plainR(row.win_mae_p50) }} / {{ plainR(row.win_mae_p75) }} / {{ plainR(row.win_mae_max) }}</td>
                  <td class="num">{{ plainR(row.loss_mfe_p50) }}</td>
                  <td class="num">{{ row.touched }} 轮 / {{ row.touched_won }} 轮</td>
                  <td class="num">{{ row.reach_15 == null ? '—' : `${row.reach_15.toFixed(0)}%` }} / {{ row.reach_25 == null ? '—' : `${row.reach_25.toFixed(0)}%` }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="section-sub">都从首笔买入价量起，以 1R（止损距离）为单位，买入后的下一根 K 线开始算到清仓当天。“最深回撤”是持有期间最低价离买入价多远，1.00R 就是盘中碰到了止损线。盈利轮的最深回撤远小于 1R，说明止损留的余地偏宽；接近 1R，说明止损再紧就会把赚钱的单子震出去。只有日线，盘中真实的最低和最高会比这里更极端。</p>
        </template>
        <div v-if="backtestCycles.length" class="table-wrap bt-cycles">
          <table>
            <thead>
              <tr>
                <th>标的</th>
                <th>建仓</th>
                <th>清仓</th>
                <th class="num">交易笔数</th>
                <th class="num">盈亏</th>
                <th>规则</th>
                <th>K线</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(cycle, index) in backtestCycles" :key="`${cycle.symbol}-${cycle.started_at}-${index}`">
                <td class="sym">{{ tickerOf(cycle.symbol) }}<template v-if="cycle.name"> · {{ cycle.name }}</template></td>
                <td>{{ clockText(cycle.started_at) }}</td>
                <td>{{ cycle.ended_at ? clockText(cycle.ended_at) : '持仓中' }}</td>
                <td class="num">
                  <a v-if="cycle.fills.length" class="trade-count-link" @click.prevent="openCycleTrades(cycle)">{{ cycle.fills.length }}</a>
                  <span v-else>0</span>
                </td>
                <td class="num" :class="pnlClass(cycle.pnl)">{{ signedMoney(cycle.pnl) }}</td>
                <td class="rule-cell">
                  <b>{{ cycle.rules.join('、') || '—' }}</b>
                  <small v-if="cycle.triggerAt"><ClockCircleOutlined /> {{ clockText(cycle.triggerAt) }} 触发</small>
                </td>
                <td>
                  <a class="kline-link" title="在K线图上查看本轮买卖点" @click.prevent="openCycleKline(cycle)">
                    <LineChartOutlined /> K线
                  </a>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="backtestCycles.length" class="section-sub">一行是一轮建仓到清仓。交易笔数是这轮里的每一笔买入和卖出，点开看价格、数量和具体时间；点“K线”把这轮的买卖点标在走势图上，B 是买入、S 是卖出。</p>
        <a-modal
          v-model:open="tradeModalOpen"
          :title="`${tickerOf(tradeModalSymbol)} 交易记录`"
          :footer="null"
          width="560px"
          wrap-class-name="mobile-sheet-modal holdings-trade-modal"
          centered
          destroy-on-close
        >
          <a-empty v-if="!tradeModalRows.length" description="暂无交易" />
          <a-steps
            v-else
            progress-dot
            direction="vertical"
            size="small"
            class="trade-steps"
          >
            <a-step
              v-for="(fill, index) in tradeModalRows"
              :key="`${fill.side}-${fill.at}-${index}`"
              :status="fill.clear ? 'error' : fill.side === 'Buy' ? 'finish' : 'error'"
            >
              <template #title>{{ fillTitle(fill) }}</template>
              <template #description>{{ fillDesc(fill) }}</template>
            </a-step>
          </a-steps>
        </a-modal>

        <a-modal
          v-model:open="klineOpen"
          :title="klineTitle"
          :footer="null"
          width="980px"
          wrap-class-name="mobile-sheet-modal bt-kline-modal"
          centered
          destroy-on-close
          @after-open-change="(open) => open && nextTick(() => (klineChart ? klineChart.resize() : renderKlineChart()))"
        >
          <div v-if="klineCycle" class="kline-summary">
            <div><span>建仓</span><b>{{ dateText(klineCycle.started_at) }}</b></div>
            <div><span>清仓</span><b>{{ klineCycle.ended_at ? dateText(klineCycle.ended_at) : '持仓中' }}</b></div>
            <div><span>周期盈亏</span><b :class="pnlClass(klineCycle.pnl)">{{ signedMoney(klineCycle.pnl) }}</b></div>
            <div><span>交易笔数</span><b>{{ klineCycle.fills.length }}</b></div>
            <div class="kline-rules"><span>触发规则</span><b>{{ klineCycle.rules.join('、') || '—' }}</b><small v-if="klineCycle.triggerAt" class="kline-trigger-time"><ClockCircleOutlined /> {{ clockText(klineCycle.triggerAt) }} 触发</small></div>
          </div>
          <div class="kline-scope">
            <span class="kline-scope-label">悬停任意日期的条件清单</span>
            <a-segmented
              v-model:value="klineChecksAll"
              size="small"
              :options="[{ label: '全部规则', value: 'all' }, { label: '仅触发', value: 'ready' }]"
            />
          </div>
          <a-spin :spinning="klineLoading">
            <a-alert v-if="klineError" type="error" :message="klineError" show-icon class="kline-alert">
              <template #action>
                <a-button size="small" type="link" @click="retryKline">重试</a-button>
              </template>
            </a-alert>
            <a-empty v-else-if="!klineLoading && !klineCandles.length" description="没有取到这段日线" />
            <div v-show="klineCandles.length" ref="klineChartRef" class="kline-chart" />
          </a-spin>
          <div v-if="klineChipFills.length" class="kline-fills">
            <span
              v-for="(fill, index) in klineChipFills"
              :key="`${fill.side}-${fill.at}-${index}`"
              class="kline-fill"
              :class="fill.side === 'Buy' ? 'is-b' : 'is-s'"
              :title="fillDesc(fill)"
            >
              <b>{{ fill.side === 'Buy' ? 'B' : 'S' }}</b>
              {{ dateText(fill.at) }} · {{ bareMoney(fill.price, 2) }} × {{ formatQty(fill.qty) }}
              <i v-if="fill.count > 1" class="kline-fill-merge">{{ fill.count }} 笔</i>
            </span>
          </div>
          <p v-if="klineExitNote" class="kline-exit-note">{{ klineExitNote }}</p>
          <p class="kline-note">B = 买入（蓝色实线），S = 卖出（红色虚线）；横线穿过当日 K 线标出成交价落在当天高低区间的哪个位置，圆点即成交价位。琥珀色闪电 ⚡ 是触发信号，标在信号日（收盘满足规则的那根 K 线）上方，当天收盘即成交。悬停<b>任意日期</b>会列出当天至少命中一条必填的规则，逐条给出「必填：」「辅助：」的 ✓/✗；必填一项都没中的规则不显示。四条卖出规则（收缩跌破、均线死叉、RSI超买、超涨回落）同样逐日列出，持仓期间任一条当天刚成立就按收盘价清仓。信号日额外标注触发，卖出日显示卖出逻辑；上方可切换「全部规则 / 仅触发」。阴影是本轮持仓区间，灰色虚线是买入均价。回测按日线收盘判断，信号当天按收盘价成交，成交时间均为日线时刻 23:00（UTC）；加仓要收盘高于均价且比上一批再高 0.25 倍 ATR。同日多条规则共振时按「收缩突破 &gt; 均线交叉 &gt; RSI超卖 &gt; 超跌反弹」取主导规则。拖动滑块或滚轮缩放可查看前后走势。</p>
        </a-modal>

        <section class="bt-rules" aria-label="回测规则">
          <h3>回测规则</h3>
          <p>收盘判断，信号当天按收盘价成交。每条规则硬条件必须全中，辅助条件按各自门槛。同一天多条规则共振时，按「收缩突破 &gt; 均线交叉 &gt; RSI超卖 &gt; 超跌反弹」取主导规则，建仓与离场都跟随它。距离、涨幅和止损都按 14 日 ATR 衡量，买入当天的 ATR 锁定为初始尺度。1R 是该规则的 ATR 止损距离。均线交叉和收缩突破先买计划仓位的 1/3，之后收盘高于均价且比上一批买价再高出至少 0.25 倍初始 ATR，当天按收盘价再买一批；另外两套一次买入、不加仓，股数按「打到 ATR 止损时亏损不超过总本金的 1%」封顶。离场：各规则 ATR 止损锚定首笔入场价（盈利加仓摊高均价后不会被正常回踩误杀），吊灯止盈与反转触及 MA20 全天候生效、不必先分批。只有趋势规则做 +1.5R / +2.5R 各减 1/3，尾仓跟随 ATR 吊灯。时间止损：均线交叉满 15 日、收缩突破满 20 日且浮盈不到 1.5 倍初始 ATR 则离场（浮盈达标自动豁免），RSI超卖满 10 日、超跌反弹满 8 日无条件离场；持仓满 40 日且四条规则都不成立也清掉。清仓后 5 个交易日才能再买。总资金 3 万：核心的标的平分 50%，卫星的标的平分 30%，其余 20% 留作现金。A 股按 100 股一手，其他市场按 1 股。A 股计低佣、印花税、过户费，并且 T+1。美股佣金 0.05%、单笔最低 1 美元、没有印花税，可以当日买卖。</p>
          <div class="bt-rule-list">
            <article class="bt-rule">
              <h4>均线交叉</h4>
              <ol>
                <li>
                  <b>硬条件</b>
                  <ol>
                    <li>MA5 不低于 MA10，允许差 0.08 倍 ATR。</li>
                    <li>近 5 日涨幅在 1.2 到 3.5 倍当日 ATR% 之间。</li>
                    <li>收盘在 MA20 上，但不超过 2.5 倍 ATR。</li>
                  </ol>
                </li>
                <li>
                  <b>辅助条件</b>
                  <ol>
                    <li>5 日量比不低于 1.2。</li>
                    <li>RSI 在 50 和 72 之间。</li>
                    <li>MA20 比昨天高。</li>
                  </ol>
                </li>
                <li>
                  <b>买入</b>
                  <ol>
                    <li>硬条件 3 项全中，辅助条件 3 项里至少中 2 项，先买计划仓位的 1/3。</li>
                    <li>之后收盘高于均价、且比上一批买价再高出至少 0.25 倍初始 ATR，当天按收盘价再买 1/3。</li>
                    <li>再满足一次同样的条件，买最后 1/3。</li>
                  </ol>
                </li>
                <li>
                  <b>卖出</b>
                  <ol>
                    <li>ATR 止损锚定首笔入场价：收盘 ≤ 首笔价 − 2 倍初始 ATR，整笔清仓。</li>
                    <li>持有满 3 日后收盘跌破 MA20 下方 0.3 倍当日 ATR，整笔清仓。</li>
                    <li>吊灯止盈：浮盈到过 2.5 倍初始 ATR 后，收盘从最高价回落 2.2 倍当日 ATR 即清仓，全天候生效、不必先分批。</li>
                    <li>分批减仓：+1.5R 减 1/3，+2.5R 再减 1/3；尾仓跟随上面的吊灯止盈。</li>
                    <li>时间止损：满 15 日且浮盈不到 1.5 倍初始 ATR 则离场，浮盈达标自动豁免。</li>
                    <li>持仓满 40 日且四条规则都不成立，清掉。</li>
                  </ol>
                </li>
              </ol>
            </article>
            <article class="bt-rule">
              <h4>RSI 超卖</h4>
              <ol>
                <li>
                  <b>硬条件</b>
                  <ol>
                    <li>收盘在 MA10 下。</li>
                    <li>RSI(6) 低于 25，或 RSI(14) 低于 35。</li>
                    <li>出现锤子线或看涨吞没。</li>
                  </ol>
                </li>
                <li>
                  <b>辅助条件</b>
                  <ol>
                    <li>5 日量比高于 1.3。</li>
                    <li>收盘比 MA20 低 1 到 3 倍 ATR。</li>
                  </ol>
                </li>
                <li><b>买入</b><p>硬条件 3 项全中，辅助条件 2 项里至少中 1 项，一次买入；股数按「打到 ATR 止损时亏损不超过总本金的 1%」反推，且不超过计划仓位。</p></li>
                <li>
                  <b>卖出</b>
                  <ol>
                    <li>ATR 止损锚定首笔入场价：收盘 ≤ 首笔价 − 1.8 倍初始 ATR，整笔清仓。</li>
                    <li>止盈：收盘 ≥ 首笔价 + 3 倍初始 ATR，或当日最高价触及 MA20，整笔清仓，全天候生效。</li>
                    <li>反转规则不做 +1.5R / +2.5R 分批减仓，一次买入、一次清掉。</li>
                    <li>时间止损：持有满 10 日无条件离场。</li>
                    <li>持仓满 40 日且四条规则都不成立，清掉。</li>
                  </ol>
                </li>
              </ol>
            </article>
            <article class="bt-rule">
              <h4>收缩突破</h4>
              <ol>
                <li>
                  <b>硬条件</b>
                  <ol>
                    <li>收盘突破前 20 日高点。</li>
                    <li>前 3 个交易日里有一天带宽不超过 3.8 倍 ATR%，或处于近 120 日最低两成。</li>
                  </ol>
                </li>
                <li>
                  <b>辅助条件</b>
                  <ol>
                    <li>20 日量比不低于 1.2。</li>
                    <li>阳线实体占振幅至少 45%。</li>
                    <li>MA20 比 5 日前高。</li>
                    <li>收盘处在近 120 日高低区间的 65% 以上。</li>
                  </ol>
                </li>
                <li>
                  <b>买入</b>
                  <ol>
                    <li>硬条件 2 项全中，辅助条件 4 项里至少中 3 项，先买计划仓位的 1/3。</li>
                    <li>之后收盘高于均价、且比上一批买价再高出至少 0.25 倍初始 ATR，当天按收盘价再买 1/3。</li>
                    <li>再满足一次同样的条件，买最后 1/3。</li>
                  </ol>
                </li>
                <li>
                  <b>卖出</b>
                  <ol>
                    <li>ATR 止损锚定首笔入场价：收盘 ≤ 首笔价 − 1.8 倍初始 ATR，整笔清仓。</li>
                    <li>收盘跌破 MA20 下方 0.3 倍当日 ATR，整笔清仓（不设持有天数门槛）。</li>
                    <li>吊灯止盈：浮盈到过 2 倍初始 ATR 后，收盘从最高价回落 2 倍当日 ATR 即清仓，全天候生效、不必先分批。</li>
                    <li>分批减仓：+1.5R 减 1/3，+2.5R 再减 1/3；尾仓跟随上面的吊灯止盈。</li>
                    <li>时间止损：满 20 日且浮盈不到 1.5 倍初始 ATR 则离场，浮盈达标自动豁免。</li>
                    <li>持仓满 40 日且四条规则都不成立，清掉。</li>
                  </ol>
                </li>
              </ol>
            </article>
            <article class="bt-rule">
              <h4>超跌反弹</h4>
              <ol>
                <li>
                  <b>硬条件</b>
                  <ol>
                    <li>收盘比 MA20 低至少 2.5 倍 ATR。</li>
                    <li>RSI(14) 低于 30，或 RSI(6) 低于 20。</li>
                  </ol>
                </li>
                <li>
                  <b>辅助条件</b>
                  <ol>
                    <li>出现锤子线或看涨吞没。</li>
                    <li>前 5 日均量低于前 10 日的 85%，并且当天量比昨天多 15%。</li>
                  </ol>
                </li>
                <li><b>买入</b><p>硬条件 2 项全中，辅助条件 2 项里至少中 1 项，一次买入；股数按「打到 ATR 止损时亏损不超过总本金的 1%」反推，且不超过计划仓位。</p></li>
                <li>
                  <b>卖出</b>
                  <ol>
                    <li>ATR 止损锚定首笔入场价：收盘 ≤ 首笔价 − 2.2 倍初始 ATR，整笔清仓。</li>
                    <li>止盈：当日最高价触及 MA20，或浮盈到过 1.5 倍初始 ATR 后收盘从最高价回落 1.2 倍当日 ATR，整笔清仓，全天候生效。</li>
                    <li>反转规则不做 +1.5R / +2.5R 分批减仓，一次买入、一次清掉。</li>
                    <li>时间止损：持有满 8 日无条件离场。</li>
                    <li>持仓满 40 日且四条规则都不成立，清掉。</li>
                  </ol>
                </li>
              </ol>
            </article>
          </div>
        </section>

        <p class="disclaimer">策略根据当前账户与复盘数据制定。仓位数字对应本金与现有规则；执行前仍需核对实际可用资金与持仓。</p>
      </template>
    </a-spin>
  </div>
</template>

<style scoped>
.strategy-page {
  --page-border: var(--border);
  --page-ink: var(--text);
  --page-muted: var(--muted);
  --page-blue: #4b6de0;
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  padding: 8px 0 32px;
  color: var(--page-ink);
}
.page-header { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 26px; }
.page-eyebrow { margin: 0 0 8px; font-size: 11px; font-weight: 600; letter-spacing: .06em; color: var(--page-muted); }
h1 { display: flex; align-items: center; gap: 10px; margin: 0; font-size: 27px; font-weight: 700; letter-spacing: -.04em; line-height: 1.3; }
.title-dot { width: 7px; height: 7px; border-radius: 50%; background: #14977d; }
.page-description { margin: 8px 0 0; font-size: 13px; color: var(--page-muted); line-height: 1.6; }
.header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 14px; }
.review-link { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 600; color: #2e7564; }
.review-link span { transition: transform .15s ease; }
.review-link:hover span { transform: translateX(2px); }
.period-range { width: 240px; height: 36px; border-radius: 9px; border-color: #dce3f4; }
.refresh-button { height: 36px; border-radius: 9px; border-color: #dce3f4; color: #4662bf; background: var(--chip); font-size: 12px; box-shadow: none; }
.err { margin-bottom: 16px; }
.loading-panel { min-height: 320px; padding: 28px; border: 1px solid var(--page-border); border-radius: 18px; }
.strategy-hero { position: relative; display: grid; grid-template-columns: minmax(0, 1.08fr) minmax(390px, .92fr); gap: 34px; align-items: center; overflow: hidden; padding: 34px; margin-bottom: 18px; border: 1px solid #dde9e5; border-radius: 20px; background: linear-gradient(122deg, #f4faf7 0%, #f8fbfe 50%, #eef3ff 100%); }
.strategy-hero::after { content: ''; position: absolute; right: -76px; top: -108px; width: 250px; height: 250px; border-radius: 50%; background: radial-gradient(circle, rgba(80, 109, 185, .10), rgba(80, 109, 185, 0)); pointer-events: none; }
.hero-copy { position: relative; z-index: 1; min-width: 0; }
.eyebrow { display: flex; align-items: center; gap: 8px; margin: 0 0 15px; font-size: 12px; font-weight: 600; color: #60766f; }
.eyebrow-dot { width: 6px; height: 6px; border-radius: 50%; background: #14977d; box-shadow: 0 0 0 4px rgba(20, 151, 125, .10); }
.hero-copy h2 { margin: 0; font-size: clamp(30px, 3.6vw, 43px); font-weight: 680; line-height: 1.24; letter-spacing: -.05em; color: #243749; }
.hero-copy h2 em { color: #21836f; font-style: normal; }
.hero-description { max-width: 600px; margin: 17px 0 0; font-size: 13px; line-height: 1.85; color: #66778b; }
.hero-meta { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 21px; }
.hero-meta span { padding: 5px 9px; border: 1px solid rgba(210, 222, 230, .9); border-radius: 6px; background: rgba(255, 255, 255, .58); font-size: 10px; color: #687b89; }
.hero-metrics { position: relative; z-index: 1; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.hero-metric { display: flex; align-items: flex-start; gap: 12px; min-width: 0; min-height: 118px; padding: 17px; border: 1px solid rgba(219, 227, 239, .95); border-radius: 14px; background: rgba(255, 255, 255, .84); box-shadow: 0 5px 18px rgba(46, 70, 105, .035); }
.metric-icon { display: grid; place-items: center; flex: 0 0 auto; width: 32px; height: 32px; border-radius: 9px; font-size: 15px; }
.metric-icon.blue { color: #4b6de0; background: #eef2ff; }
.metric-icon.teal { color: #14977d; background: #eaf7f3; }
.metric-icon.amber { color: #b78631; background: #fcf5e8; }
.metric-icon.violet { color: #8a68c9; background: #f3effb; }
.hero-metric > div { min-width: 0; }
.hero-metric p { margin: 1px 0 7px; font-size: 10px; color: #7c8798; }
.hero-metric strong { display: block; font-size: 21px; line-height: 1.22; letter-spacing: -.035em; font-variant-numeric: tabular-nums; color: var(--text); white-space: nowrap; }
.hero-metric strong small { font-size: 11px; font-weight: 500; letter-spacing: 0; color: #8490a2; }
.hero-metric > div > span { display: block; margin-top: 6px; font-size: 9px; line-height: 1.5; color: #929baa; }
.strategy-nav { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin: 24px 0 4px; padding: 9px 12px; border: 1px solid #edf0f6; border-radius: 10px; background: var(--chip); font-size: 12px; }
.nav-label { margin: 0 12px 0 4px; color: #97a0b0; }
.strategy-nav a { padding: 6px 10px; border-radius: 6px; color: #65728a; transition: background .15s, color .15s; }
.strategy-nav a:hover { color: #2d7564; background: #eaf6f2; }
.strategy-nav .nav-next { margin-left: auto; color: #4863b6; font-weight: 600; }
.strategy-nav .nav-next span { margin-left: 4px; }
.section-title { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 34px 0 8px; font-size: 19px; font-weight: 650; letter-spacing: -.025em; scroll-margin-top: 200px; }
.section-number { padding: 5px 6px; border-radius: 5px; background: #eaf5f1; color: #3c8a77; font-size: 11px; font-weight: 600; letter-spacing: 0; font-variant-numeric: tabular-nums; }
.section-badge { display: inline-flex; align-items: center; padding: 3px 8px; border-radius: 5px; background: var(--chip); color: var(--muted); font-size: 11px; font-weight: 400; letter-spacing: 0; }
.section-sub { margin: 0 0 18px; font-size: 12px; line-height: 1.7; color: var(--muted); }
.backtest-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: -6px 0 16px; }
.backtest-run { margin: 0; }
.bt-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 8px; }
.bt-result { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-bottom: 12px; }
.bt-cycles { margin: 14px 0 8px; }
.bt-cycles table { min-width: 760px; }
.bt-cycles td, .bt-cycles th { white-space: nowrap; }
.bt-sub { margin: 22px 0 0; color: #34465d; font-size: 13px; font-weight: 650; }
.bt-cycles td.bt-note { min-width: 260px; white-space: normal; }
.bt-thin { margin-left: 6px; color: #8993a5; font-size: 10px; font-weight: 400; }
.bt-total td { border-top: 1px solid rgba(137, 147, 165, 0.35); font-weight: 650; }
.rule-cell b { display: block; color: #34465d; font-size: 12px; font-weight: 650; }
.rule-cell small { display: inline-flex; align-items: center; gap: 4px; margin-top: 3px; color: #8993a5; font-size: 10px; font-variant-numeric: tabular-nums; }
.rule-cell small .anticon { font-size: 10px; }
.bt-result .hero-metric { min-height: 0; }
.trade-count-link { color: #2962ff; font-weight: 650; cursor: pointer; }
.trade-count-link:hover { text-decoration: underline; }
.hero-metric strong .trade-count-link { font: inherit; letter-spacing: inherit; color: #2962ff; }
.trade-steps { max-height: 60vh; overflow: auto; padding: 4px 8px 0 0; }
.trade-steps :deep(.ant-steps-item-title) { font-size: 13px; font-weight: 650; line-height: 1.45; }
.trade-steps :deep(.ant-steps-item-description) { font-size: 12px; color: var(--muted); padding-bottom: 12px; }
.kline-link { display: inline-flex; align-items: center; gap: 5px; color: #2962ff; font-weight: 650; cursor: pointer; white-space: nowrap; }
.kline-link:hover { text-decoration: underline; }
.kline-summary { display: grid; grid-template-columns: repeat(4, auto) minmax(0, 1fr); gap: 8px 22px; margin-bottom: 12px; padding: 12px 16px; border: 1px solid #e5ebf3; border-radius: 12px; background: linear-gradient(150deg, #fbfcff, #f7f9fc); }
.kline-summary > div { min-width: 0; }
.kline-summary span { display: block; margin-bottom: 3px; color: #8993a5; font-size: 10px; }
.kline-summary b { display: block; overflow: hidden; color: #34465d; font-size: 13px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; }
.kline-trigger-time { display: inline-flex; align-items: center; gap: 4px; margin-top: 4px; color: #14977d; font-size: 10px; font-weight: 600; font-variant-numeric: tabular-nums; }
.kline-trigger-time .anticon { font-size: 10px; }
.kline-chart { width: 100%; height: 460px; }
.kline-scope { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 10px; }
.kline-scope-label { color: var(--muted); font-size: 11px; }
.kline-alert { margin-bottom: 12px; }
.kline-fills { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.kline-fill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 9px 4px 5px; border: 1px solid #e3e8f1; border-radius: 999px; background: #f9fafc; color: #5c6b78; font-size: 11px; font-variant-numeric: tabular-nums; }
.kline-fill b { display: grid; place-items: center; width: 17px; height: 17px; border-radius: 50%; color: #fff; font-size: 10px; font-weight: 700; }
.kline-fill.is-b b { background: #2962ff; }
.kline-fill.is-s b { background: #f23645; }
.kline-fill-merge { padding: 1px 6px; border-radius: 999px; background: rgba(242, 54, 69, 0.12); color: #d62735; font-size: 10px; font-style: normal; font-weight: 650; }
.kline-fill.is-b .kline-fill-merge { background: rgba(41, 98, 255, 0.12); color: #2962ff; }
.kline-exit-note { margin: 10px 0 0; padding: 9px 12px; border: 1px solid #f0d9a4; border-radius: 10px; background: linear-gradient(150deg, #fff9ec, #fdf3dd); color: #8a6116; font-size: 11px; line-height: 1.75; }
.kline-note { margin: 10px 0 0; color: var(--muted); font-size: 11px; line-height: 1.7; }
.bt-rules { margin: 18px 0 0; }
.bt-rules h3 { margin: 0 0 8px; font-size: 14px; font-weight: 650; letter-spacing: -.02em; }
.bt-rules > p { margin: 0 0 12px; color: var(--muted); font-size: 12px; line-height: 1.75; }
.bt-rule-list { display: grid; gap: 12px; }
.bt-rule { padding: 14px 18px 6px; border: 1px solid #dfe8e4; border-radius: 14px; background: #f8fbfa; }
.bt-rule h4 { margin: 0 0 4px; font-size: 13px; font-weight: 650; }
.bt-rule > ol, .bt-rule ol ol { margin: 0; padding: 0; list-style: none; }
.bt-rule > ol { counter-reset: group; }
.bt-rule > ol > li { display: grid; grid-template-columns: 22px minmax(0, 1fr); gap: 0 10px; padding: 10px 0; border-bottom: 1px solid #e7eeeb; color: var(--muted); font-size: 12px; line-height: 1.7; counter-increment: group; }
.bt-rule > ol > li:last-child { border-bottom: 0; }
.bt-rule > ol > li::before { content: counter(group); grid-row: 1 / span 2; display: grid; place-items: center; width: 22px; height: 22px; border: 1px solid #d8e6e1; border-radius: 6px; background: var(--panel); color: #628d81; font-size: 11px; font-weight: 650; }
.bt-rule > ol > li > b { color: #4a5c7b; font-weight: 650; }
.bt-rule > ol > li > p { grid-column: 2; margin: 2px 0 0; }
.bt-rule ol ol { grid-column: 2; counter-reset: item; }
.bt-rule ol ol li { display: grid; grid-template-columns: 18px minmax(0, 1fr); gap: 4px; margin-top: 4px; counter-increment: item; }
.bt-rule ol ol li::before { content: counter(item) "."; color: #628d81; font-weight: 650; font-variant-numeric: tabular-nums; }

/* ---- strategy digest ---- */
.digest { position: relative; overflow: hidden; margin: 18px 0 6px; padding: 26px 28px 28px; border: 1px solid #d5e6e0; border-radius: 20px; background: linear-gradient(158deg, #f1faf6 0%, #f6fbfd 44%, #eff3ff 100%); box-shadow: 0 10px 30px rgba(30, 60, 90, .05); scroll-margin-top: 200px; }
.digest::before { content: ''; position: absolute; inset: 0 0 auto; height: 3px; background: linear-gradient(90deg, #14977d 0%, #089981 20%, #4b6de0 46%, #8a68c9 68%, #f5a623 86%, #f23645 100%); }
.digest::after { content: ''; position: absolute; right: -90px; bottom: -130px; width: 300px; height: 300px; border-radius: 50%; background: radial-gradient(circle, rgba(20, 151, 125, .10), rgba(20, 151, 125, 0)); pointer-events: none; }
.digest > * { position: relative; z-index: 1; }
.digest-top { display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between; gap: 16px; }
.digest-eyebrow { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; font-size: 11px; font-weight: 650; letter-spacing: .04em; color: #4d7a6e; }
.digest-pulse { width: 7px; height: 7px; border-radius: 50%; background: #14977d; animation: digestPulse 2.4s ease-out infinite; }
@keyframes digestPulse { 0% { box-shadow: 0 0 0 0 rgba(20, 151, 125, .4); } 70% { box-shadow: 0 0 0 9px rgba(20, 151, 125, 0); } 100% { box-shadow: 0 0 0 0 rgba(20, 151, 125, 0); } }
.digest-title h2 { margin: 0; font-size: 25px; font-weight: 690; letter-spacing: -.04em; color: #1f3a44; }
.digest-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 9px 0 0; font-size: 11px; color: #6f8478; font-variant-numeric: tabular-nums; }
.digest-meta i { width: 3px; height: 3px; border-radius: 50%; background: #a9bdb5; }
.digest-tools { display: flex; flex-wrap: wrap; gap: 8px; }
.digest-btn { height: 34px; border-radius: 9px; border-color: #cfe0da; background: rgba(255, 255, 255, .92); color: #357061; font-size: 12px; font-weight: 600; box-shadow: none; transition: transform .16s ease, box-shadow .16s ease; }
.digest-btn:hover { transform: translateY(-1px); border-color: #14977d; color: #14977d; box-shadow: 0 6px 16px rgba(20, 151, 125, .15); }
.digest-btn.primary { border-color: transparent; background: linear-gradient(120deg, #14977d, #2f7f96); color: #fff; }
.digest-btn.primary:hover { border-color: transparent; color: #fff; box-shadow: 0 8px 20px rgba(20, 151, 125, .3); }
.digest-statement { margin: 18px 0 0; padding: 15px 18px; border-left: 3px solid #14977d; border-radius: 0 12px 12px 0; background: rgba(255, 255, 255, .74); color: #33474f; font-size: 13px; line-height: 1.9; }
.digest-params { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-top: 16px; }
.dparam { display: flex; align-items: flex-start; gap: 10px; min-width: 0; padding: 14px; border: 1px solid rgba(214, 226, 236, .95); border-radius: 13px; background: rgba(255, 255, 255, .88); transition: transform .16s ease, box-shadow .16s ease; }
.dparam:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(40, 66, 100, .08); }
.dparam-icon { display: grid; place-items: center; flex: 0 0 auto; width: 28px; height: 28px; border-radius: 8px; font-size: 13px; }
.dparam.teal .dparam-icon { color: #14977d; background: #e6f6f1; }
.dparam.blue .dparam-icon { color: #4b6de0; background: #ecf0ff; }
.dparam.violet .dparam-icon { color: #8a68c9; background: #f2eefb; }
.dparam.amber .dparam-icon { color: #b78631; background: #fbf4e7; }
.dparam.red .dparam-icon { color: #d63c4c; background: #fdedee; }
.dparam > div { min-width: 0; }
.dparam p { margin: 0 0 5px; font-size: 10px; color: #7d8a97; }
.dparam strong { display: block; font-size: 18px; line-height: 1.2; letter-spacing: -.035em; color: #243749; font-variant-numeric: tabular-nums; white-space: nowrap; }
.dparam strong small { font-size: 11px; font-weight: 500; letter-spacing: 0; color: #8490a2; }
.dparam > div > span { display: block; margin-top: 5px; font-size: 9px; line-height: 1.55; color: #939dab; }
.digest-split { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.06fr); gap: 14px; margin-top: 14px; }
.digest-block { min-width: 0; margin-top: 14px; padding: 18px; border: 1px solid rgba(216, 228, 238, .95); border-radius: 15px; background: rgba(255, 255, 255, .84); }
.digest-split .digest-block { margin-top: 0; }
.digest-block h3 { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 14px; font-size: 14px; font-weight: 670; letter-spacing: -.02em; color: #263c4a; }
.dblock-tag { padding: 3px 8px; border-radius: 6px; background: linear-gradient(120deg, #e4f4ef, #eaf0ff); color: #2f7565; font-size: 10px; font-weight: 700; letter-spacing: .02em; }
.dblock-note { color: #8b96a4; font-size: 10px; font-weight: 500; }
.dblock-count { margin-left: auto; padding: 3px 9px; border-radius: 999px; background: #f0f3fa; color: #5b6b84; font-size: 10px; font-weight: 650; font-variant-numeric: tabular-nums; }
.dblock-count.good { background: #e6f6f1; color: #1a7c67; }
.dblock-toggle { margin-left: auto; color: #2962ff; font-size: 11px; font-weight: 600; cursor: pointer; }
.dblock-toggle:hover { text-decoration: underline; }
.digest-sleeves { display: grid; gap: 13px; }
.dsleeve-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.dsleeve-head b { font-size: 12px; font-weight: 650; color: #35485c; }
.dsleeve-nums { font-size: 12px; font-weight: 650; color: #46586f; font-variant-numeric: tabular-nums; }
.dsleeve-nums em { margin: 0 3px; color: #a5b1c0; font-style: normal; }
.dsleeve-track { position: relative; height: 16px; margin-top: 7px; overflow: hidden; border-radius: 6px; background: #eef2f7; }
.dsleeve-target { position: absolute; inset: 0 auto 0 0; border-radius: 6px; background: repeating-linear-gradient(115deg, rgba(122, 142, 163, .26) 0 4px, rgba(122, 142, 163, .07) 4px 9px); }
.dsleeve-now { position: absolute; left: 0; top: 4px; bottom: 4px; border-radius: 4px; transition: width .5s cubic-bezier(.22, .7, .3, 1); }
.dsleeve.core .dsleeve-now { background: linear-gradient(90deg, #2f6f62, #4d8f7d); }
.dsleeve.satellite .dsleeve-now { background: linear-gradient(90deg, #4b6de0, #7592ea); }
.dsleeve.cash .dsleeve-now { background: linear-gradient(90deg, #b78631, #d8ab5b); }
.dsleeve-foot { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px; margin-top: 6px; font-size: 10px; color: #8a95a3; font-variant-numeric: tabular-nums; }
.dsleeve-foot em { color: #5f7183; font-style: normal; font-weight: 600; }
.digest-warn, .digest-ok { display: flex; align-items: flex-start; gap: 7px; margin: 14px 0 0; padding: 9px 12px; border-radius: 10px; font-size: 11px; line-height: 1.7; }
.digest-warn { border: 1px solid #f0d9a4; background: #fdf6e7; color: #8a6116; }
.digest-ok { border: 1px solid #cfe7dd; background: #f0f9f5; color: #2c7563; }
.digest-actions { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.daction { display: grid; grid-template-columns: 26px minmax(0, 1fr) auto; align-items: center; gap: 11px; padding: 11px 13px; border: 1px solid #e4e9f1; border-left-width: 3px; border-radius: 11px; background: #fbfcfe; transition: transform .16s ease, box-shadow .16s ease; }
.daction:hover { transform: translateX(2px); box-shadow: 0 6px 16px rgba(40, 66, 100, .07); }
.daction-count { display: grid; place-items: center; width: 26px; height: 26px; border-radius: 8px; color: #fff; font-size: 12px; font-weight: 700; font-variant-numeric: tabular-nums; }
.daction b { display: block; font-size: 12px; font-weight: 650; color: #33465b; }
.daction > div > span { display: block; margin-top: 3px; font-size: 10px; line-height: 1.6; color: #8b95a4; word-break: break-word; }
.daction-go { display: inline-flex; align-items: center; gap: 4px; color: #2962ff; font-size: 10px; font-weight: 600; white-space: nowrap; }
.daction-go:hover { text-decoration: underline; }
.daction.danger { border-left-color: #f23645; }
.daction.danger .daction-count { background: linear-gradient(140deg, #b81a28, #c62a38); }
.daction.warn { border-left-color: #f5a623; }
.daction.warn .daction-count { background: linear-gradient(140deg, #f0ab3c, #f7c46e); color: #4a3208; }
.daction.info { border-left-color: #4b6de0; }
.daction.info .daction-count { background: linear-gradient(140deg, #2f49ad, #3f5cc9); }
.daction.muted { border-left-color: #a9b4c4; }
.daction.muted .daction-count { background: linear-gradient(140deg, #dde3ec, #e9eef5); color: #46566b; }
.daction.good { border-left-color: #14977d; }
.daction.good .daction-count { background: linear-gradient(140deg, #0f7561, #2b9279); }
.digest-rules { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.drule { display: flex; flex-direction: column; min-width: 0; padding: 14px; border: 1px solid #e2e8f0; border-top: 3px solid #4b6de0; border-radius: 12px; background: #fbfcfe; transition: transform .16s ease, box-shadow .16s ease; }
.drule:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(40, 66, 100, .08); }
.drule.reversion { border-top-color: #8a68c9; }
.drule-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; }
.drule-head b { font-size: 13px; font-weight: 680; color: #2a3d52; }
.drule-kind { padding: 2px 6px; border-radius: 5px; background: #ecf0ff; color: #4b6de0; font-size: 9px; font-weight: 650; }
.drule.reversion .drule-kind { background: #f3effb; color: #7d5bbd; }
.drule-gate { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; margin: 9px 0 0; font-size: 10px; color: #6d7b8c; font-variant-numeric: tabular-nums; }
.drule-gate em { padding: 1px 5px; border-radius: 4px; background: #eef2f7; color: #56657a; font-style: normal; font-weight: 650; }
.drule-gate i { width: 3px; height: 3px; border-radius: 50%; background: #c3ccd8; }
.drule-body { display: none; margin-top: 11px; padding-top: 11px; border-top: 1px dashed #dfe6ef; }
.drule.open .drule-body { display: block; }
.drule-label { margin: 0 0 5px; font-size: 9px; font-weight: 700; letter-spacing: .04em; color: #8894a3; }
.drule-list { margin: 0 0 10px; padding: 0; list-style: none; counter-reset: dcond; }
.drule-list li { display: grid; grid-template-columns: 15px minmax(0, 1fr); gap: 6px; margin-top: 4px; font-size: 10px; line-height: 1.65; color: #5d6c7e; counter-increment: dcond; }
.drule-list li::before { content: counter(dcond); display: grid; place-items: center; width: 15px; height: 15px; border-radius: 4px; font-size: 9px; font-weight: 700; }
.drule-list.core li::before { background: #e6f6f1; color: #1a7c67; }
.drule-list.aux li::before { background: #f0f3fa; color: #6b7889; }
.drule-exit { margin: 0; padding: 9px 10px; border-radius: 8px; background: #f6f8fb; font-size: 10px; line-height: 1.7; color: #6a7787; }
.drule-exit b { display: block; margin-bottom: 3px; color: #4a5c7b; font-size: 9px; letter-spacing: .04em; }
.drule-exit-list { margin: 0; padding: 0; list-style: none; counter-reset: dexit; }
.drule-exit-list li { display: grid; grid-template-columns: 15px minmax(0, 1fr); gap: 6px; margin-top: 4px; counter-increment: dexit; }
.drule-exit-list li::before { content: counter(dexit); display: grid; place-items: center; width: 15px; height: 15px; border-radius: 4px; background: #e9eef6; color: #5a6a80; font-size: 9px; font-weight: 700; }
.drule-more { margin-top: 11px; color: #2962ff; font-size: 10px; font-weight: 600; cursor: pointer; }
.drule-more:hover { text-decoration: underline; }
.digest-exit { margin-top: 14px; padding: 15px 16px; border: 1px solid #dfe8e4; border-radius: 12px; background: linear-gradient(120deg, #f7fbf9, #f8faff); }
.dexit-title { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 680; color: #2f7565; }
.dexit-steps { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 11px; }
.dexit { display: flex; flex-direction: column; gap: 2px; padding: 8px 13px; border: 1px solid; border-radius: 10px; background: var(--panel); }
.dexit b { font-size: 14px; font-weight: 700; letter-spacing: -.03em; font-variant-numeric: tabular-nums; }
.dexit em { font-size: 9px; font-style: normal; color: #8a95a3; }
.dexit.stop { border-color: #f7ccd1; }
.dexit.stop b { color: #d62735; }
.dexit.take { border-color: #cfe7dd; }
.dexit.take b { color: #12866f; }
.dexit.trail { border-color: #d8dff0; }
.dexit.trail b { color: #4b6de0; }
.dexit-arrow { color: #b6c1cf; font-size: 11px; }
.dexit-foot { margin: 11px 0 0; font-size: 10px; line-height: 1.75; color: #7d8a98; }
.digest-score { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.dscore { min-width: 0; padding: 14px; border: 1px solid #e4e9f1; border-radius: 12px; background: #fbfcfe; }
.dscore-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.dscore-head b { font-size: 11px; font-weight: 650; color: #3a4c60; }
.dscore-flag { flex: 0 0 auto; padding: 2px 7px; border-radius: 999px; background: #fdedee; color: #cf2b39; font-size: 9px; font-weight: 700; }
.dscore.pass .dscore-flag { background: #e6f6f1; color: #1a7c67; }
.dscore-value { display: flex; align-items: baseline; gap: 7px; margin: 9px 0 0; font-size: 22px; font-weight: 700; letter-spacing: -.04em; color: #263b4e; font-variant-numeric: tabular-nums; }
.dscore-value small { font-size: 10px; font-weight: 500; letter-spacing: 0; color: #939dab; }
.dscore-track { height: 5px; margin-top: 10px; overflow: hidden; border-radius: 3px; background: #eef2f7; }
.dscore-fill { height: 100%; border-radius: 3px; background: linear-gradient(90deg, #e0596a, #f5a623); transition: width .6s cubic-bezier(.22, .7, .3, 1); }
.dscore.pass .dscore-fill { background: linear-gradient(90deg, #12866f, #35a68c); }
.dscore-hint { display: block; margin-top: 8px; font-size: 9px; line-height: 1.6; color: #939dab; }
.digest-copy-hint { margin: 0 0 10px; font-size: 12px; line-height: 1.75; color: var(--muted); }
.digest-copy-area { display: block; width: 100%; min-height: 300px; max-height: 52vh; padding: 12px 14px; border: 1px solid #d9e3ec; border-radius: 10px; background: #f8fafc; color: #35485c; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; line-height: 1.75; resize: vertical; }
.digest-copy-area:focus { outline: none; border-color: #14977d; box-shadow: 0 0 0 3px rgba(20, 151, 125, .12); }
.digest-copy-tools { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-top: 12px; }
@media (max-width: 1100px) {
  .digest-params { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .digest-rules, .digest-score { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 800px) {
  .digest-split { grid-template-columns: minmax(0, 1fr); }
  .digest-split .digest-block + .digest-block { margin-top: 14px; }
}
@media (max-width: 560px) {
  .digest { padding: 20px 16px 22px; border-radius: 16px; }
  .digest-title h2 { font-size: 21px; }
  .digest-tools { width: 100%; }
  .digest-tools .digest-btn { flex: 1 1 auto; }
  .digest-statement { padding: 13px 14px; font-size: 12px; line-height: 1.85; }
  .digest-params, .digest-rules, .digest-score { grid-template-columns: minmax(0, 1fr); }
  .digest-block { padding: 15px; }
  .daction { grid-template-columns: 24px minmax(0, 1fr); }
  .daction-go { grid-column: 2; }
  .dexit { flex: 1 1 calc(50% - 8px); }
  .dexit-arrow { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .digest-pulse { animation: none; }
  .dsleeve-now, .dscore-fill { transition: none; }
}
@media (max-width: 1100px) {
  .bt-result { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.alloc-card { padding: 24px; border: 1px solid #dfe8e4; border-radius: 16px; background: linear-gradient(180deg, #fbfdfc, #f7fafb); }
.alloc-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.alloc-heading p { margin: 0 0 7px; font-size: 14px; font-weight: 650; color: #354c5e; }
.alloc-heading span { font-size: 11px; line-height: 1.6; color: #8490a0; }
.alloc-heading > strong { color: #296e60; font-size: 26px; line-height: 1; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
.alloc-heading > strong small { margin-left: 2px; font-size: 11px; font-weight: 500; color: #8797a2; }
.alloc-bar { display: flex; gap: 4px; height: 38px; margin-top: 8px; overflow: hidden; border-radius: 9px; }
.alloc-kicker { margin: 16px 0 0; font-size: 11px; font-weight: 650; color: #7b8b98; }
.alloc-names { display: grid; gap: 8px; margin-top: 16px; }
.alloc-sleeve { width: 72px; }
.alloc-name { display: grid; grid-template-columns: 72px 72px 1fr 52px 1.2fr; gap: 8px; align-items: center; padding: 8px 2px; border-top: 1px solid #e7eeeb; font-size: 12px; color: #5c6b78; font-variant-numeric: tabular-nums; }
.alloc-name b { color: #24384a; font-weight: 650; }
.seg { display: flex; align-items: center; justify-content: center; min-width: 0; overflow: hidden; color: var(--panel); font-size: 11px; font-weight: 600; white-space: nowrap; }
.seg.core { flex: 6; background: linear-gradient(90deg, #3d6b61, #4d7c70); }
.seg.sat { flex: 3; background: linear-gradient(90deg, #6a9aaa, #86adba); }
.seg.cash { flex: 1; background: #dfe9e7; color: #5d716f; }
.alloc-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
.alloc-item { display: flex; align-items: flex-start; gap: 11px; min-width: 0; padding: 15px; border: 1px solid #e5ece9; border-radius: 11px; background: rgba(255, 255, 255, .72); }
.alloc-index { display: grid; place-items: center; flex: 0 0 auto; width: 25px; height: 25px; border-radius: 7px; background: #edf3f1; color: #598074; font-size: 9px; font-weight: 650; font-variant-numeric: tabular-nums; }
.alloc-item p { margin: 0 0 6px; font-size: 12px; font-weight: 650; color: #425969; }
.alloc-item p strong { color: #2f7565; font-weight: 700; }
.alloc-item > div > span { display: block; font-size: 10px; line-height: 1.7; color: var(--muted); }
.strat-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.strat { position: relative; display: flex; flex-direction: column; gap: 14px; min-width: 0; padding: 22px; overflow: hidden; border: 1px solid var(--page-border); border-radius: 14px; background: var(--panel); box-shadow: 0 4px 16px rgba(38, 58, 95, .025); transition: transform .18s ease, box-shadow .18s ease; }
.strat:hover { transform: translateY(-2px); box-shadow: 0 9px 24px rgba(38, 58, 95, .07); }
.strat::before { content: ''; position: absolute; top: 0; left: 22px; width: 32px; height: 2px; border-radius: 0 0 4px 4px; background: #6b9e91; }
.norm { align-self: flex-start; padding: 4px 8px; border-radius: 5px; background: #edf5f2; color: #4e8276; font-size: 10px; font-weight: 600; }
.strat h3 { margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -.02em; }
.strat ul, .strat ol { margin: 0 0 4px; padding-left: 17px; font-size: 12px; line-height: 1.85; color: var(--muted); }
.strat li { margin-bottom: 10px; padding-left: 2px; }
.strat li:last-child { margin-bottom: 0; }
.strat li::marker { color: #91aeaa; }
.strat li b { color: #4a5a72; font-weight: 600; }
.risk-guide { display: flex; flex-direction: column; gap: 10px; padding: 13px; border: 1px solid #dce9e4; border-radius: 10px; background: linear-gradient(145deg, #f3f9f6, #f9fbfe); }
.risk-definition { display: flex; align-items: center; gap: 10px; }
.risk-symbol { display: grid; place-items: center; flex: 0 0 auto; width: 32px; height: 32px; border-radius: 8px; background: #3f8b78; color: var(--panel); font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; box-shadow: 0 4px 10px rgba(63, 139, 120, .18); }
.risk-definition > div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.risk-definition b { color: #3f5960; font-size: 11px; font-weight: 650; }
.risk-definition span:last-child { color: #72858e; font-size: 10px; line-height: 1.55; }
.risk-levels { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
.risk-level { display: flex; flex-direction: column; min-width: 0; padding: 8px 9px; border: 1px solid #e3ece8; border-radius: 7px; background: rgba(255, 255, 255, .82); }
.risk-level span { color: #788c91; font-size: 9px; font-weight: 600; font-variant-numeric: tabular-nums; }
.risk-level b { margin: 2px 0; color: #3e7164; font-size: 14px; line-height: 1.2; font-variant-numeric: tabular-nums; }
.risk-level small { color: #8a999e; font-size: 9px; }
.risk-level.stop b { color: #b45d68; }
.risk-follow { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.risk-follow span { display: flex; flex-direction: column; gap: 2px; padding: 8px 9px; border-radius: 7px; background: rgba(232, 242, 249, .72); }
.risk-follow b { color: #526a76; font-size: 9px; font-weight: 650; }
.risk-follow em { color: #7f909a; font-size: 9px; font-style: normal; line-height: 1.45; }
.risk-example { margin: 0; color: #788b93; font-size: 9px; line-height: 1.65; }
.reentry-guide { display: flex; flex-direction: column; gap: 10px; padding: 13px; border: 1px solid #e1e7f2; border-radius: 10px; background: linear-gradient(145deg, #f6f8fd, #fbfcfe); }
.reentry-head { display: flex; align-items: center; gap: 9px; }
.reentry-icon { display: grid; place-items: center; flex: 0 0 auto; width: 28px; height: 28px; border-radius: 8px; background: #edf1fb; color: #5d70a8; font-size: 12px; }
.reentry-head > div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.reentry-head b { color: #4e5d76; font-size: 11px; font-weight: 650; }
.reentry-head span:last-child { color: #8490a4; font-size: 9px; line-height: 1.45; }
.reentry-flow { display: flex; align-items: center; gap: 4px; }
.reentry-flow span { flex: 1; min-width: 0; padding: 5px 3px; border: 1px solid #e3e8f2; border-radius: 5px; background: rgba(255, 255, 255, .86); color: #596a83; font-size: 9px; line-height: 1.35; text-align: center; white-space: nowrap; }
.reentry-flow i { color: #aab3c3; font-size: 9px; font-style: normal; }
.reentry-cases { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.reentry-cases > div { padding: 8px 9px; border-radius: 7px; }
.reentry-cases .fall { background: #fcf1f3; }
.reentry-cases .rise { background: #edf7f3; }
.reentry-cases b { display: block; margin-bottom: 2px; color: #9b5a66; font-size: 9px; font-weight: 650; }
.reentry-cases .rise b { color: #427d6e; }
.reentry-cases span { display: block; color: #84919d; font-size: 9px; line-height: 1.5; }
.payoff-guide { display: flex; flex-direction: column; gap: 9px; padding: 12px; border: 1px solid #e4e7f2; border-radius: 10px; background: #f8f9fd; }
.payoff-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.payoff-head b { color: #56637c; font-size: 11px; font-weight: 650; }
.payoff-head span { color: #8d97aa; font-size: 9px; white-space: nowrap; }
.payoff-equation { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto auto; align-items: end; gap: 6px; }
.payoff-equation > div { min-width: 0; }
.payoff-equation span { display: block; margin-bottom: 2px; color: #8f99ad; font-size: 9px; }
.payoff-equation b { display: block; color: #536078; font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.payoff-equation i { padding-bottom: 1px; color: #a2abba; font-size: 10px; font-style: normal; }
.payoff-equation > strong { color: #6b63a5; font-size: 18px; line-height: 1; font-variant-numeric: tabular-nums; }
.payoff-guide p { margin: 0; color: #8893a5; font-size: 9px; line-height: 1.65; }
.strat .foot { margin: auto 0 0; padding-top: 13px; border-top: 1px solid #edf0f5; font-size: 11px; line-height: 1.75; color: var(--muted); }
.strat .foot a { color: #4c75a0; font-weight: 600; }
.table-wrap { overflow-x: auto; border: 1px solid var(--page-border); border-radius: 14px; background: var(--panel); }
table { width: 100%; min-width: 650px; border-collapse: collapse; background: var(--panel); font-size: 12px; }
th { padding: 14px 20px; border-bottom: 1px solid var(--page-border); background: var(--chip); color: #8792a7; font-size: 11px; font-weight: 500; text-align: left; white-space: nowrap; }
td { padding: 16px 20px; border-bottom: 1px solid #edf0f5; color: #66758b; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: #fafcfb; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.symbol-name { display: flex; align-items: center; gap: 10px; }
.ticker-avatar { display: grid; place-items: center; flex-shrink: 0; width: 30px; height: 30px; border: 1px solid #e4ece9; border-radius: 9px; background: #f2f8f6; color: #5d8d81; font-size: 12px; font-weight: 650; }
.sym { color: #43536c; font-weight: 650; font-variant-numeric: tabular-nums; }
.pill { display: inline-block; padding: 4px 9px; border-radius: 5px; font-size: 11px; white-space: nowrap; }
.pill.cut { background: #fceff1; color: #ba5463; }
.pill.trim { background: #fcf5e8; color: #a77a30; }
.pill.hold { background: #eaf6f2; color: #388a74; }
.note { display: block; margin-top: 6px; color: #98a1b0; font-size: 10px; }
.empty { padding: 32px; color: var(--muted); text-align: center; }
.checklist { margin: 0; padding: 6px 24px; border: 1px solid #dfe8e4; border-radius: 14px; background: #f8fbfa; list-style: none; counter-reset: chk; }
.checklist li { display: grid; grid-template-columns: 28px minmax(0, 1fr); align-items: start; gap: 14px; padding: 16px 0; border-bottom: 1px solid #e7eeeb; color: var(--muted); font-size: 12px; line-height: 1.8; counter-increment: chk; }
.checklist li:last-child { border-bottom: 0; }
.checklist li::before { content: counter(chk, decimal-leading-zero); display: grid; place-items: center; width: 25px; height: 25px; border: 1px solid #d8e6e1; border-radius: 7px; background: var(--panel); color: #628d81; font-size: 10px; font-weight: 600; }
.checklist b { color: #4a5c7b; font-weight: 600; }
.disclaimer { margin: 22px 0 0; padding: 0 2px; color: #96a0b2; font-size: 11px; line-height: 1.8; }

.execution-overview {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.execution-stat {
  min-width: 0;
  padding: 16px 18px;
  border: 1px solid #e5ebf3;
  border-radius: 14px;
  background: linear-gradient(150deg, #fbfcff, #f7f9fc);
}
.execution-stat > span {
  display: block;
  margin-bottom: 5px;
  color: #8993a5;
  font-size: 10px;
}
.execution-stat > strong {
  display: block;
  color: #34465d;
  font-size: 21px;
  line-height: 1.25;
  letter-spacing: -.025em;
  font-variant-numeric: tabular-nums;
}
.execution-stat > strong small {
  color: #8b95a7;
  font-size: 10px;
  font-weight: 500;
}
.execution-stat > small {
  display: block;
  margin-top: 5px;
  color: #9aa3b2;
  font-size: 10px;
  line-height: 1.5;
}
.execution-stat.alert {
  border-color: #f1dcda;
  background: linear-gradient(150deg, #fff9f8, #fff4f2);
}
.execution-stat.alert > strong { color: #ba5a57; }
.execution-stat.alert > small { color: #bd817d; }

.exit-playbook {
  position: relative;
  overflow: hidden;
  margin-bottom: 18px;
  padding: 22px 24px;
  border: 1px solid #e3e8f0;
  border-radius: 16px;
  background:
    radial-gradient(circle at 100% 0, rgba(226, 235, 255, .8), transparent 34%),
    linear-gradient(135deg, #fbfcfd, #f7f9fc);
}
.playbook-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
}
.playbook-kicker {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 7px;
  color: #61708b;
  font-size: 10px;
  font-weight: 650;
  letter-spacing: .04em;
}
.playbook-kicker :deep(svg) { color: #667bba; }
.playbook-heading h3 {
  margin: 0;
  color: #314157;
  font-size: 16px;
  font-weight: 650;
  letter-spacing: -.02em;
}
.playbook-heading > p {
  max-width: 360px;
  margin: 0;
  color: #8792a5;
  font-size: 10px;
  line-height: 1.7;
  text-align: right;
}
.workflow-steps {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 20px minmax(0, 1fr) 20px minmax(0, 1fr) 20px minmax(0, 1fr);
  align-items: stretch;
  gap: 6px;
  margin-top: 18px;
}
.workflow-step {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
  padding: 13px;
  border: 1px solid #e8ecf3;
  border-radius: 11px;
  background: rgba(255, 255, 255, .82);
}
.workflow-step > i {
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 7px;
  background: #edf2fa;
  color: #6075a4;
  font-size: 9px;
  font-style: normal;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.workflow-step > span { min-width: 0; }
.workflow-step b {
  display: block;
  margin-bottom: 4px;
  color: #4a596f;
  font-size: 11px;
  font-weight: 650;
}
.workflow-step em {
  display: block;
  color: #929bac;
  font-size: 9px;
  font-style: normal;
  line-height: 1.6;
}
.workflow-arrow {
  align-self: center;
  color: #b7c1d1;
  font-size: 11px;
}

.position-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.position-card {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  padding: 19px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--panel);
  box-shadow: 0 8px 24px rgba(49, 67, 95, .045);
  transition: border-color .15s ease, box-shadow .15s ease, transform .15s ease;
}
.position-card:hover {
  transform: translateY(-1px);
  border-color: #d8e1ee;
  box-shadow: 0 12px 28px rgba(49, 67, 95, .07);
}
.position-card.overdue {
  border-color: #eed6d3;
  background: linear-gradient(155deg, #fffafa 0%, #fff 58%);
}
.position-card.overdue::before {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: linear-gradient(180deg, #c25553, #e19a95);
}
.position-card.trim {
  border-color: #eee0c7;
  background: linear-gradient(155deg, #fffdf7 0%, #fff 58%);
}
.position-card.warning {
  border-color: #efd39a;
  background: linear-gradient(155deg, #fffdf8 0%, #fff 58%);
}
.position-card.warning:hover { border-color: #e3bd73; }
.position-card.warning::before {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: linear-gradient(180deg, #d59a24, #e8c171);
}
.position-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.position-ident { display: flex; flex-direction: column; min-width: 0; }
.position-ident .sym { font-size: 13px; letter-spacing: -.015em; }
.position-ident small {
  margin-top: 3px;
  color: #98a1b0;
  font-size: 9px;
  white-space: nowrap;
}
.execution-pill {
  flex: 0 0 auto;
  padding: 4px 8px;
  border-radius: 999px;
  font-size: 9px;
  font-weight: 650;
  white-space: nowrap;
}
.execution-pill.cut { border: 1px solid #f0d6d3; background: #fff2f0; color: #b74f4d; }
.execution-pill.warning { border: 1px solid #efd39a; background: #fffbf1; color: #a56c00; }
.execution-pill.trim { border: 1px solid #eddfc7; background: #fff9ee; color: #9b752f; }
.execution-pill.hold { border: 1px solid #d9ebe5; background: #f0faf6; color: #3f8873; }
.position-numbers {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  margin: 14px 0 11px;
  border: 1px solid #edf0f5;
  border-radius: 10px;
  background: #edf0f5;
}
.position-numbers > div {
  min-width: 0;
  padding: 10px 11px;
  background: #fafbfd;
}
.position-numbers span,
.position-plan span {
  display: block;
  margin-bottom: 3px;
  color: #98a2b1;
  font-size: 9px;
}
.position-numbers strong {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px;
  color: #4c5d74;
  font-size: 13px;
  font-weight: 650;
  line-height: 1.35;
  font-variant-numeric: tabular-nums;
}
.position-numbers strong em {
  color: #8a95a7;
  font-size: 9px;
  font-style: normal;
  font-weight: 500;
}
.position-plan {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.position-plan > div {
  min-width: 0;
  padding: 9px 10px;
  border: 1px solid #edf0f5;
  border-radius: 9px;
  background: #fcfdfe;
}
.position-plan strong {
  display: block;
  overflow: hidden;
  color: #53627a;
  font-size: 11px;
  font-weight: 650;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.position-plan > div.suggestion-stop {
  border-color: #efd39a;
  background: #fffbf1;
  box-shadow: inset 3px 0 0 #d59a24;
}
.position-plan > div.mandatory-stop {
  border-color: #eec5c1;
  background: #fff7f6;
  box-shadow: inset 3px 0 0 #bf3b32;
}
.position-plan .suggestion-stop > span { color: #9f6d1e; }
.position-plan .mandatory-stop > span { color: #a54139; }
.position-plan .suggestion-stop strong { color: #a56c00; }
.position-plan .mandatory-stop strong { color: #b52f27; font-weight: 750; }
.position-plan .suggestion-stop small { color: #a48655; }
.position-plan .mandatory-stop small { color: #b47772; }
.position-plan small {
  display: block;
  overflow: hidden;
  margin-top: 3px;
  color: #a0a9b7;
  font-size: 8px;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.position-plan small.due { color: #b76a3f; font-weight: 600; }
.position-action-note {
  flex: 1;
  margin: 12px 0 0;
  padding: 10px 11px;
  border-radius: 9px;
  background: #f3f8f6;
  color: #62786f;
  font-size: 10px;
  line-height: 1.7;
}
.overdue .position-action-note { background: #fff4f2; color: #9d625f; }
.position-action-note.suggestion-exit-note { border: 1px solid #efd39a; background: #fffbf1; color: #a56c00; }
.trim .position-action-note { background: #fff9ee; color: #8b713f; }
.position-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #eff1f5;
}
.position-card-foot > span {
  flex: 1;
  color: #9ba4b3;
  font-size: 9px;
  line-height: 1.6;
}
.clear-action {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: 0 0 auto;
  padding: 7px 11px;
  border-radius: 8px;
  background: #b85a56;
  color: var(--panel);
  font-size: 10px;
  font-weight: 650;
  box-shadow: 0 4px 10px rgba(184, 90, 86, .16);
  transition: background .15s ease, transform .15s ease;
}
.clear-action:hover { color: var(--panel); background: #a84d49; transform: translateX(1px); }
.position-card.trim .clear-action { background: #a87d37; box-shadow: 0 4px 10px rgba(168, 125, 55, .14); }
.position-card.trim .clear-action:hover { background: #986f2e; }
.position-card.warning .clear-action { background: #c9942c; box-shadow: 0 4px 10px rgba(201, 148, 44, .2); }
.position-card.warning .clear-action:hover { background: #b58320; }
.muted-action { color: #607797; font-size: 10px; font-weight: 600; white-space: nowrap; }
.muted-action:hover { color: #365d88; }

.reentry-callout {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  margin-bottom: 13px;
  padding: 17px 19px;
  border: 1px solid #e3e8f1;
  border-radius: 14px;
  background: #f9fafc;
}
.reentry-callout.positive {
  border-color: #d6eae3;
  background: linear-gradient(130deg, #f3faf7, #f8fbfa);
}
.callout-icon {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: #edf1f7;
  color: #7b889e;
  font-size: 17px;
}
.reentry-callout.positive .callout-icon { background: #e4f5ef; color: #388a71; }
.reentry-callout b { display: block; margin-bottom: 4px; color: #46566d; font-size: 12px; }
.reentry-callout p { max-width: 680px; margin: 0; color: #828d9f; font-size: 10px; line-height: 1.7; }
.callout-rule {
  padding: 7px 9px;
  border: 1px dashed #d9e3dc;
  border-radius: 7px;
  background: rgba(255, 255, 255, .65);
  color: #71877d;
  font-size: 9px;
  white-space: nowrap;
}
.candidate-filter {
  display: flex;
  gap: 5px;
  overflow-x: auto;
  margin-bottom: 14px;
  padding: 4px;
  border: 1px solid #e9edf4;
  border-radius: 11px;
  background: #f7f9fc;
  scrollbar-width: thin;
}
.candidate-filter button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
  padding: 7px 11px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #7a869a;
  font: inherit;
  font-size: 10px;
  cursor: pointer;
  transition: background .15s ease, color .15s ease, box-shadow .15s ease;
}
.candidate-filter button:hover { color: #4c5f78; background: var(--panel); }
.candidate-filter button.active {
  background: var(--panel);
  color: #415b80;
  font-weight: 650;
  box-shadow: 0 2px 7px rgba(54, 68, 91, .08);
}
.candidate-filter button span {
  display: grid;
  place-items: center;
  min-width: 18px;
  height: 17px;
  padding: 0 5px;
  border-radius: 999px;
  background: #edf0f5;
  color: #8994a6;
  font-size: 8px;
}
.candidate-filter button.active span { background: #e8f3ef; color: #4b8976; }
.candidate-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 13px;
}
.candidate-card {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  padding: 17px;
  border: 1px solid var(--border);
  border-radius: 15px;
  background: var(--panel);
  box-shadow: 0 7px 20px rgba(49, 67, 95, .04);
}
.candidate-card.ready { border-color: #d8e8e2; background: linear-gradient(160deg, #fcfffe, #fff); }
.candidate-card.cooling { border-color: #eadfc9; background: linear-gradient(160deg, #fffdfa, #fff); }
.candidate-card.muted { background: #fbfcfd; }
.candidate-card::before {
  content: '';
  position: absolute;
  inset: 0 0 auto;
  height: 2px;
  background: #dde3ed;
}
.candidate-card.ready::before { background: linear-gradient(90deg, #55a58d, #8fc9b8); }
.candidate-card.cooling::before { background: linear-gradient(90deg, #c9a461, #e2c892); }
.candidate-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.candidate-avatar { width: 32px; height: 32px; border-color: #dceae5; background: #f0f8f5; }
.candidate-status {
  flex: 0 0 auto;
  padding: 4px 7px;
  border-radius: 999px;
  font-size: 8px;
  font-weight: 650;
  white-space: nowrap;
}
.candidate-status.ready { background: #e9f7f2; color: #3b8c73; }
.candidate-status.cooling { background: #fcf5e8; color: #9b7836; }
.candidate-status.watching { background: #f1f4fa; color: #6e7f9c; }
.candidate-status.muted { background: #f2f3f6; color: #8f98a8; }
.candidate-signal {
  margin-top: 13px;
  padding: 11px 12px;
  border: 1px solid #edf0f5;
  border-radius: 10px;
  background: #fafbfd;
}
.candidate-signal span { display: block; margin-bottom: 4px; color: #9aa3b2; font-size: 8px; }
.candidate-signal b { display: block; color: #4c5d74; font-size: 12px; font-weight: 650; line-height: 1.45; }
.candidate-signal small { display: block; margin-top: 4px; color: #9ba5b4; font-size: 8px; }
.candidate-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px;
  margin-top: 10px;
}
.candidate-metrics > div { min-width: 0; padding: 8px 9px; border-radius: 8px; background: var(--chip); }
.candidate-metrics span { display: block; margin-bottom: 2px; color: #9da6b5; font-size: 8px; }
.candidate-metrics b {
  display: block;
  overflow: hidden;
  color: #55657c;
  font-size: 10px;
  font-weight: 650;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.candidate-metrics em {
  display: block;
  margin-top: 2px;
  color: #98a1af;
  font-size: 8px;
  font-style: normal;
  font-weight: 650;
  line-height: 1.4;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.cycle-result {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-top: 11px;
  padding-top: 11px;
  border-top: 1px dashed #e9edf3;
}
.cycle-result > span { color: #939dac; font-size: 8px; white-space: nowrap; }
.cycle-result > b { color: #5e6b80; font-size: 10px; font-weight: 650; text-align: right; font-variant-numeric: tabular-nums; }
.cycle-result > b em { margin-left: 4px; font-size: 8px; font-style: normal; font-weight: 500; }
.candidate-note {
  flex: 1;
  margin: 10px 0 0;
  color: #8994a5;
  font-size: 9px;
  line-height: 1.65;
}
.manual-check {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  margin-top: 11px;
  padding: 9px 10px;
  border-radius: 8px;
  font-size: 8px;
  line-height: 1.6;
}
.manual-check :deep(svg) { flex: 0 0 auto; margin-top: 2px; font-size: 11px; }
.manual-check b { display: block; margin-bottom: 2px; font-size: 9px; font-weight: 650; }
.manual-check.ready { background: #eef9f5; color: #588477; }
.manual-check.ready b { color: #347a65; }
.manual-check.cooling { background: #fdf8ee; color: #9a845b; }
.manual-check.cooling b { color: #95702b; }
.manual-check.muted { background: #f5f7fa; color: #8b96a7; }
.candidate-empty { margin-top: 4px; }

.empty-panel {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 22px;
  border: 1px dashed #dfe6ee;
  border-radius: 14px;
  background: #fbfcfd;
}
.empty-mark {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: 10px;
  background: #f0f4f7;
  color: #8996a7;
  font-size: 17px;
}
.empty-panel b { display: block; margin-bottom: 4px; color: #687488; font-size: 11px; }
.empty-panel p { margin: 0; color: #99a2b1; font-size: 10px; line-height: 1.6; }

@media (max-width: 1100px) {
  .strategy-hero { grid-template-columns: 1fr; gap: 26px; padding: 28px; }
  .hero-metrics { max-width: 720px; }
  .alloc-list { grid-template-columns: 1fr 1fr; }
  .strat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 800px) {
  .strategy-page { padding-top: 2px; }
  .page-header { align-items: flex-start; }
  .header-actions { flex-direction: column; align-items: flex-end; gap: 10px; }
  .period-range { width: 100%; max-width: 240px; }
  .strategy-hero { padding: 24px; }
  .bt-grid { grid-template-columns: 1fr; }
  .section-title { scroll-margin-top: 24px; }
  .alloc-list { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  .page-header { flex-direction: column; gap: 16px; margin-bottom: 20px; }
  h1 { font-size: 24px; }
  .page-description { font-size: 12px; }
  .header-actions { width: 100%; flex-direction: row; align-items: center; justify-content: space-between; }
  .review-link { display: none; }
  .strategy-hero { padding: 22px 18px; border-radius: 16px; }
  .hero-copy h2 { font-size: clamp(28px, 8.3vw, 37px); }
  .hero-description { font-size: 12px; }
  .hero-meta { gap: 6px; }
  .hero-meta span { padding: 4px 7px; font-size: 9px; }
  .hero-metrics { gap: 8px; }
  .hero-metric { min-height: 108px; padding: 13px; gap: 9px; }
  .metric-icon { width: 27px; height: 27px; font-size: 13px; }
  .hero-metric p { font-size: 9px; }
  .hero-metric strong { font-size: 17px; }
  .hero-metric > div > span { font-size: 8px; }
  .strategy-nav { margin-top: 18px; gap: 4px; padding: 8px; font-size: 11px; }
  .nav-label { display: none; }
  .strategy-nav a { flex: 1; padding: 6px 4px; text-align: center; white-space: nowrap; }
  .strategy-nav .nav-next { display: none; }
  .section-title { margin-top: 28px; gap: 8px; font-size: 17px; }
  .section-sub { font-size: 11px; }
  .alloc-card { padding: 18px 16px; border-radius: 13px; }
  .alloc-name { grid-template-columns: 64px 72px 1fr auto; }
  .alloc-name span:nth-child(4) { display: none; }
  .alloc-heading > strong { font-size: 22px; }
  .alloc-bar { height: 32px; }
  .seg.cash span { display: none; }
  .alloc-item { padding: 13px; }
  .strat-grid { grid-template-columns: 1fr; gap: 12px; }
  .strat { padding: 20px; border-radius: 12px; }
  .reentry-flow { gap: 3px; }
  .reentry-flow span { padding-inline: 2px; font-size: 8.5px; }
  th, td { padding: 12px 14px; }
  .checklist { padding: 4px 16px; }
  .checklist li { gap: 10px; font-size: 12px; }
  .kline-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; padding: 12px 14px; }
  .kline-summary .kline-rules { grid-column: 1 / -1; }
  .kline-chart { height: 380px; }
}

@media (max-width: 1100px) {
  .position-grid { grid-template-columns: 1fr; }
  .candidate-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .workflow-steps { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
  .workflow-arrow { display: none; }
  .position-plan { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 800px) {
  .execution-overview { gap: 8px; }
  .execution-stat { padding: 13px 14px; }
  .execution-stat > strong { font-size: 18px; }
  .playbook-heading { align-items: flex-start; flex-direction: column; gap: 7px; }
  .playbook-heading > p { max-width: none; text-align: left; }
  .reentry-callout { grid-template-columns: 36px minmax(0, 1fr); }
  .callout-rule { grid-column: 2; width: fit-content; }
  .candidate-grid { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  .execution-overview { grid-template-columns: 1fr; }
  .execution-stat { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; column-gap: 10px; padding: 11px 13px; }
  .execution-stat > span { margin-bottom: 0; }
  .execution-stat > strong { grid-row: span 2; font-size: 17px; text-align: right; }
  .execution-stat > small { grid-column: 1; margin-top: 2px; }
  .exit-playbook { padding: 18px 16px; border-radius: 13px; }
  .playbook-heading h3 { font-size: 14px; }
  .workflow-steps { grid-template-columns: 1fr; gap: 7px; margin-top: 14px; }
  .workflow-step { padding: 11px; }
  .position-card { padding: 15px; border-radius: 13px; }
  .position-card-head { align-items: flex-start; }
  .execution-pill { font-size: 8px; }
  .position-numbers > div { padding: 9px 8px; }
  .position-numbers strong { font-size: 11px; }
  .position-plan { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
  .position-card-foot { align-items: flex-start; flex-direction: column; }
  .clear-action { width: 100%; justify-content: center; }
  .reentry-callout { grid-template-columns: 1fr; gap: 10px; padding: 15px; }
  .callout-icon { width: 31px; height: 31px; }
  .callout-rule { grid-column: 1; width: 100%; overflow-x: auto; text-align: center; }
  .candidate-filter { margin-inline: -2px; }
  .candidate-filter button { padding: 7px 9px; }
  .candidate-card { padding: 15px; border-radius: 13px; }
  .candidate-metrics { gap: 5px; }
  .candidate-metrics > div { padding: 7px 6px; }
  .candidate-metrics b { font-size: 9px; }
  .empty-panel { padding: 18px; }
}

:global(html[data-theme='dark'] .strategy-hero) {
  background: #1a1e27;
  border-color: var(--border);
}
:global(html[data-theme='dark'] .strategy-hero::after) { background: none; }
:global(html[data-theme='dark'] .hero-meta span),
:global(html[data-theme='dark'] .hero-metric),
:global(html[data-theme='dark'] .alloc-item),
:global(html[data-theme='dark'] .risk-level),
:global(html[data-theme='dark'] .reentry-flow span),
:global(html[data-theme='dark'] .workflow-step),
:global(html[data-theme='dark'] .callout-rule),
:global(html[data-theme='dark'] .candidate-signal),
:global(html[data-theme='dark'] .position-numbers > div),
:global(html[data-theme='dark'] .position-plan > div) {
  background: #262b36;
  border-color: var(--border);
  box-shadow: none;
}
:global(html[data-theme='dark'] .alloc-card),
:global(html[data-theme='dark'] .risk-guide),
:global(html[data-theme='dark'] .reentry-guide),
:global(html[data-theme='dark'] .payoff-guide),
:global(html[data-theme='dark'] .checklist),
:global(html[data-theme='dark'] .bt-rule),
:global(html[data-theme='dark'] .execution-stat),
:global(html[data-theme='dark'] .exit-playbook),
:global(html[data-theme='dark'] .reentry-callout),
:global(html[data-theme='dark'] .candidate-filter),
:global(html[data-theme='dark'] .empty-panel) {
  background: #1e222d;
  border-color: var(--border);
}
:global(html[data-theme='dark'] .metric-icon.blue),
:global(html[data-theme='dark'] .metric-icon.teal),
:global(html[data-theme='dark'] .metric-icon.amber),
:global(html[data-theme='dark'] .metric-icon.violet),
:global(html[data-theme='dark'] .section-number),
:global(html[data-theme='dark'] .alloc-index),
:global(html[data-theme='dark'] .norm),
:global(html[data-theme='dark'] .ticker-avatar),
:global(html[data-theme='dark'] .reentry-icon),
:global(html[data-theme='dark'] .workflow-step > i),
:global(html[data-theme='dark'] .callout-icon),
:global(html[data-theme='dark'] .reentry-callout.positive .callout-icon),
:global(html[data-theme='dark'] .candidate-avatar),
:global(html[data-theme='dark'] .empty-mark),
:global(html[data-theme='dark'] .candidate-filter button span),
:global(html[data-theme='dark'] .candidate-filter button.active span) {
  background: #2a2e39;
  color: var(--muted);
}
:global(html[data-theme='dark'] .position-card.overdue),
:global(html[data-theme='dark'] .position-card.trim),
:global(html[data-theme='dark'] .position-card.warning),
:global(html[data-theme='dark'] .candidate-card.ready),
:global(html[data-theme='dark'] .candidate-card.cooling),
:global(html[data-theme='dark'] .candidate-card.muted),
:global(html[data-theme='dark'] .reentry-callout.positive) {
  background: #1e222d;
  border-color: var(--border);
}
:global(html[data-theme='dark'] .execution-stat.alert) {
  background: rgba(242, 54, 69, 0.1);
  border-color: rgba(242, 54, 69, 0.35);
}
:global(html[data-theme='dark'] .position-plan > div.suggestion-stop),
:global(html[data-theme='dark'] .position-action-note.suggestion-exit-note),
:global(html[data-theme='dark'] .trim .position-action-note) {
  background: rgba(230, 167, 0, 0.12);
  border-color: rgba(230, 167, 0, 0.35);
}
:global(html[data-theme='dark'] .position-plan > div.mandatory-stop),
:global(html[data-theme='dark'] .overdue .position-action-note) {
  background: rgba(242, 54, 69, 0.12);
  border-color: rgba(242, 54, 69, 0.35);
}
:global(html[data-theme='dark'] .position-action-note),
:global(html[data-theme='dark'] .reentry-cases .rise),
:global(html[data-theme='dark'] .manual-check.ready) {
  background: rgba(8, 153, 129, 0.12);
  color: #8ecfb8;
}
:global(html[data-theme='dark'] .reentry-cases .fall) { background: rgba(242, 54, 69, 0.12); }
:global(html[data-theme='dark'] .manual-check.cooling) { background: rgba(230, 167, 0, 0.12); color: #e2c48a; }
:global(html[data-theme='dark'] .manual-check.muted),
:global(html[data-theme='dark'] .risk-follow span) { background: #262b36; }
:global(html[data-theme='dark'] .pill.cut),
:global(html[data-theme='dark'] .execution-pill.cut) {
  background: rgba(242, 54, 69, 0.16);
  border-color: rgba(242, 54, 69, 0.35);
  color: #ff8b95;
}
:global(html[data-theme='dark'] .pill.trim),
:global(html[data-theme='dark'] .execution-pill.trim),
:global(html[data-theme='dark'] .execution-pill.warning),
:global(html[data-theme='dark'] .candidate-status.cooling) {
  background: rgba(230, 167, 0, 0.16);
  border-color: rgba(230, 167, 0, 0.35);
  color: #f0c14d;
}
:global(html[data-theme='dark'] .pill.hold),
:global(html[data-theme='dark'] .execution-pill.hold),
:global(html[data-theme='dark'] .candidate-status.ready) {
  background: rgba(8, 153, 129, 0.16);
  border-color: rgba(8, 153, 129, 0.35);
  color: #5dbea8;
}
:global(html[data-theme='dark'] .candidate-status.watching),
:global(html[data-theme='dark'] .candidate-status.muted) {
  background: #2a2e39;
  color: var(--muted);
}
:global(html[data-theme='dark'] .seg.cash) { background: #363a45; color: var(--muted); }
:global(html[data-theme='dark'] .position-numbers) { background: var(--border); border-color: var(--border); }
:global(html[data-theme='dark'] .strategy-nav a:hover),
:global(html[data-theme='dark'] .strategy-page tbody tr:hover),
:global(html[data-theme='dark'] .candidate-filter button.active) {
  background: #262b36;
  color: var(--text);
}
:global(html[data-theme='dark'] .hero-copy h2),
:global(html[data-theme='dark'] .alloc-heading p),
:global(html[data-theme='dark'] .alloc-name b),
:global(html[data-theme='dark'] .alloc-item p),
:global(html[data-theme='dark'] .execution-stat > strong),
:global(html[data-theme='dark'] .playbook-heading h3),
:global(html[data-theme='dark'] .workflow-step b),
:global(html[data-theme='dark'] .reentry-callout b),
:global(html[data-theme='dark'] .candidate-signal b),
:global(html[data-theme='dark'] .candidate-metrics b),
:global(html[data-theme='dark'] .cycle-result > b),
:global(html[data-theme='dark'] .sym),
:global(html[data-theme='dark'] .checklist b),
:global(html[data-theme='dark'] .bt-rules h3),
:global(html[data-theme='dark'] .bt-rule h4),
:global(html[data-theme='dark'] .bt-rule > ol > li > b),
:global(html[data-theme='dark'] .strat li b),
:global(html[data-theme='dark'] .payoff-equation b),
:global(html[data-theme='dark'] .payoff-equation > strong),
:global(html[data-theme='dark'] .reentry-head b),
:global(html[data-theme='dark'] .risk-definition b),
:global(html[data-theme='dark'] .position-numbers strong),
:global(html[data-theme='dark'] .position-plan strong),
:global(html[data-theme='dark'] .empty-panel b) {
  color: var(--text);
}
:global(html[data-theme='dark'] .hero-copy h2 em),
:global(html[data-theme='dark'] .review-link),
:global(html[data-theme='dark'] .alloc-heading > strong),
:global(html[data-theme='dark'] .alloc-item p strong),
:global(html[data-theme='dark'] .section-number),
:global(html[data-theme='dark'] .risk-level b),
:global(html[data-theme='dark'] .strat .foot a),
:global(html[data-theme='dark'] .manual-check.ready),
:global(html[data-theme='dark'] .manual-check.ready b) {
  color: #5dbea8;
}
:global(html[data-theme='dark'] .eyebrow),
:global(html[data-theme='dark'] .hero-description),
:global(html[data-theme='dark'] .hero-meta span),
:global(html[data-theme='dark'] .nav-label),
:global(html[data-theme='dark'] .strategy-nav a),
:global(html[data-theme='dark'] .strategy-nav .nav-next),
:global(html[data-theme='dark'] .strategy-page td),
:global(html[data-theme='dark'] .strategy-page th),
:global(html[data-theme='dark'] .disclaimer),
:global(html[data-theme='dark'] .reentry-flow span),
:global(html[data-theme='dark'] .callout-rule) {
  color: var(--muted);
}
:global(html[data-theme='dark'] .bt-rule > ol > li::before) {
  border-color: var(--border);
  background: #2a2e39;
  color: #8ecfb8;
}
:global(html[data-theme='dark'] .bt-rule ol ol li::before) { color: #8ecfb8; }
:global(html[data-theme='dark'] .alloc-name),
:global(html[data-theme='dark'] .checklist li),
:global(html[data-theme='dark'] .bt-rule > ol > li),
:global(html[data-theme='dark'] .strat .foot),
:global(html[data-theme='dark'] .strategy-page td),
:global(html[data-theme='dark'] .position-card-foot),
:global(html[data-theme='dark'] .cycle-result) {
  border-color: var(--border);
}
:global(html[data-theme='dark'] .kline-summary) {
  background: #1e222d;
  border-color: var(--border);
}
:global(html[data-theme='dark'] .kline-summary b) { color: var(--text); }
:global(html[data-theme='dark'] .kline-summary span) { color: var(--muted); }
:global(html[data-theme='dark'] .bt-sub) { color: var(--text); }
:global(html[data-theme='dark'] .bt-thin) { color: var(--muted); }
:global(html[data-theme='dark'] .rule-cell b) { color: var(--text); }
:global(html[data-theme='dark'] .rule-cell small) { color: var(--muted); }
:global(html[data-theme='dark'] .kline-trigger-time) { color: #4fd1b5; }
:global(html[data-theme='dark'] .kline-fill) {
  background: #262b36;
  border-color: var(--border);
  color: var(--muted);
}
:global(html[data-theme='dark'] .kline-fill-merge) { background: rgba(242, 54, 69, 0.2); color: #ff8a92; }
:global(html[data-theme='dark'] .kline-fill.is-b .kline-fill-merge) { background: rgba(41, 98, 255, 0.22); color: #8fb0ff; }
:global(html[data-theme='dark'] .kline-exit-note) { border-color: #5c4a22; background: linear-gradient(150deg, #2a2416, #241f12); color: #e0b566; }
:global(html[data-theme='dark'] .kline-link) { color: #6f9bff; }

/* ---- strategy digest · dark ---- */
:global(html[data-theme='dark'] .digest) { border-color: #31463f; background: linear-gradient(158deg, #17251f 0%, #1a2230 46%, #1e2136 100%); box-shadow: 0 12px 32px rgba(0, 0, 0, .34); }
:global(html[data-theme='dark'] .digest::after) { background: radial-gradient(circle, rgba(20, 151, 125, .18), rgba(20, 151, 125, 0)); }
:global(html[data-theme='dark'] .digest-eyebrow) { color: #6fc4ae; }
:global(html[data-theme='dark'] .digest-title h2) { color: #e2eae6; }
:global(html[data-theme='dark'] .digest-meta) { color: #8fa69c; }
:global(html[data-theme='dark'] .digest-meta i) { background: #4e6b62; }
:global(html[data-theme='dark'] .digest-btn) { border-color: #3a4d47; background: #212b28; color: #7fd3bd; }
:global(html[data-theme='dark'] .digest-btn:hover) { border-color: #35a68c; color: #93e2cd; }
:global(html[data-theme='dark'] .digest-btn.primary) { border-color: transparent; background: linear-gradient(120deg, #17705d, #2a6f83); color: #eafff8; }
:global(html[data-theme='dark'] .digest-statement) { border-left-color: #35a68c; background: rgba(30, 42, 38, .82); color: #c6d4cf; }
:global(html[data-theme='dark'] .dparam),
:global(html[data-theme='dark'] .digest-block),
:global(html[data-theme='dark'] .drule),
:global(html[data-theme='dark'] .dscore) { border-color: var(--border); background: #1e222d; }
:global(html[data-theme='dark'] .dparam.teal .dparam-icon) { color: #4fd1b5; background: #1b322c; }
:global(html[data-theme='dark'] .dparam.blue .dparam-icon) { color: #8fb0ff; background: #1e2540; }
:global(html[data-theme='dark'] .dparam.violet .dparam-icon) { color: #bda3ec; background: #262038; }
:global(html[data-theme='dark'] .dparam.amber .dparam-icon) { color: #e0b566; background: #322a1a; }
:global(html[data-theme='dark'] .dparam.red .dparam-icon) { color: #ff8a92; background: #351e21; }
:global(html[data-theme='dark'] .dparam p),
:global(html[data-theme='dark'] .dparam > div > span) { color: var(--muted); }
:global(html[data-theme='dark'] .dparam strong) { color: var(--text); }
:global(html[data-theme='dark'] .dparam strong small) { color: var(--muted); }
:global(html[data-theme='dark'] .digest-block h3) { color: #dbe4e0; }
:global(html[data-theme='dark'] .dblock-tag) { background: linear-gradient(120deg, #1b322c, #1e2540); color: #6fd0b7; }
:global(html[data-theme='dark'] .dblock-note) { color: var(--muted); }
:global(html[data-theme='dark'] .dblock-count) { background: #2a2e39; color: #a8b3c2; }
:global(html[data-theme='dark'] .dblock-count.good) { background: #1b322c; color: #4fd1b5; }
:global(html[data-theme='dark'] .dblock-toggle),
:global(html[data-theme='dark'] .drule-more),
:global(html[data-theme='dark'] .daction-go) { color: #6f9bff; }
:global(html[data-theme='dark'] .dsleeve-head b) { color: #cbd6d1; }
:global(html[data-theme='dark'] .dsleeve-nums) { color: #b6c2ce; }
:global(html[data-theme='dark'] .dsleeve-nums em) { color: #6c7a89; }
:global(html[data-theme='dark'] .dsleeve-track),
:global(html[data-theme='dark'] .dscore-track) { background: #2a2e39; }
:global(html[data-theme='dark'] .dsleeve-target) { background: repeating-linear-gradient(115deg, rgba(150, 168, 188, .3) 0 4px, rgba(150, 168, 188, .08) 4px 9px); }
:global(html[data-theme='dark'] .dsleeve-foot) { color: var(--muted); }
:global(html[data-theme='dark'] .dsleeve-foot em) { color: #a9b6c4; }
:global(html[data-theme='dark'] .digest-warn) { border-color: #5c4a22; background: #2a2416; color: #e0b566; }
:global(html[data-theme='dark'] .digest-ok) { border-color: #2c4d43; background: #17281f; color: #6cc6ad; }
:global(html[data-theme='dark'] .daction) { border-color: var(--border); background: #232833; }
:global(html[data-theme='dark'] .daction b) { color: #d3dcd8; }
:global(html[data-theme='dark'] .daction > div > span) { color: var(--muted); }
:global(html[data-theme='dark'] .daction.muted) { border-left-color: #6b7686; }
:global(html[data-theme='dark'] .daction.danger .daction-count) { background: linear-gradient(140deg, #ff7b85, #ff9aa2); color: #3d0d12; }
:global(html[data-theme='dark'] .daction.warn .daction-count) { background: linear-gradient(140deg, #e8b45c, #f2cd8c); color: #3d2c07; }
:global(html[data-theme='dark'] .daction.info .daction-count) { background: linear-gradient(140deg, #7fa2ff, #a3bcff); color: #131f3d; }
:global(html[data-theme='dark'] .daction.muted .daction-count) { background: linear-gradient(140deg, #4a5464, #5c6779); color: #e6ecf4; }
:global(html[data-theme='dark'] .daction.good .daction-count) { background: linear-gradient(140deg, #3fb99e, #63cdb4); color: #08251f; }
:global(html[data-theme='dark'] .drule-head b) { color: #d7e0dc; }
:global(html[data-theme='dark'] .drule-kind) { background: #1e2540; color: #8fb0ff; }
:global(html[data-theme='dark'] .drule.reversion .drule-kind) { background: #262038; color: #bda3ec; }
:global(html[data-theme='dark'] .drule-gate) { color: #9aa6b4; }
:global(html[data-theme='dark'] .drule-gate em) { background: #2a2e39; color: #adb8c6; }
:global(html[data-theme='dark'] .drule-gate i) { background: #4a5464; }
:global(html[data-theme='dark'] .drule-body) { border-top-color: var(--border); }
:global(html[data-theme='dark'] .drule-label) { color: var(--muted); }
:global(html[data-theme='dark'] .drule-list li) { color: #a7b3c0; }
:global(html[data-theme='dark'] .drule-list.core li::before) { background: #1b322c; color: #4fd1b5; }
:global(html[data-theme='dark'] .drule-list.aux li::before) { background: #2a2e39; color: #9aa6b4; }
:global(html[data-theme='dark'] .drule-exit) { background: #262b36; color: #9daab8; }
:global(html[data-theme='dark'] .drule-exit b) { color: #b3c0cf; }
:global(html[data-theme='dark'] .drule-exit-list li::before) { background: #2f3542; color: #a8b4c4; }
:global(html[data-theme='dark'] .digest-exit) { border-color: #31463f; background: linear-gradient(120deg, #1a2620, #1c2230); }
:global(html[data-theme='dark'] .dexit-title) { color: #6fd0b7; }
:global(html[data-theme='dark'] .dexit.stop) { border-color: #5a2a30; }
:global(html[data-theme='dark'] .dexit.stop b) { color: #ff8a92; }
:global(html[data-theme='dark'] .dexit.take) { border-color: #2c4d43; }
:global(html[data-theme='dark'] .dexit.take b) { color: #4fd1b5; }
:global(html[data-theme='dark'] .dexit.trail) { border-color: #333c55; }
:global(html[data-theme='dark'] .dexit.trail b) { color: #8fb0ff; }
:global(html[data-theme='dark'] .dexit em),
:global(html[data-theme='dark'] .dexit-foot) { color: var(--muted); }
:global(html[data-theme='dark'] .dexit-arrow) { color: #4e5867; }
:global(html[data-theme='dark'] .dscore-head b) { color: #cbd6d1; }
:global(html[data-theme='dark'] .dscore-flag) { background: #351e21; color: #ff8a92; }
:global(html[data-theme='dark'] .dscore.pass .dscore-flag) { background: #1b322c; color: #4fd1b5; }
:global(html[data-theme='dark'] .dscore-value) { color: var(--text); }
:global(html[data-theme='dark'] .dscore-value small),
:global(html[data-theme='dark'] .dscore-hint) { color: var(--muted); }
:global(html[data-theme='dark'] .digest-copy-area) { border-color: var(--border); background: #191d27; color: #c3ced9; }
:global(html[data-theme='dark'] .digest-copy-area:focus) { border-color: #35a68c; box-shadow: 0 0 0 3px rgba(53, 166, 140, .18); }
</style>
