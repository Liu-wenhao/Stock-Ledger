<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import * as echarts from 'echarts'
import { ReloadOutlined, CalendarOutlined, AimOutlined, SwapOutlined, SafetyOutlined, PieChartOutlined } from '@ant-design/icons-vue'
import { fetchTradingReview } from '@/api/portfolio'
import { formatMoney, formatPct, pnlClass, tickerOf, toNumber } from '@/utils/format'
import {
  axisLabelStyle,
  axisLineStyle,
  moneyAxisFormatter,
  noSplitLine,
} from '@/utils/chart'

const data = ref(null)
const loading = ref(true)
const error = ref('')
const barRef = ref(null)
const countRef = ref(null)
const symbolRef = ref(null)
let barChart
let countChart
let symbolChart

const GREEN = '#14977d'
const RED = '#e35d6a'

const capital = computed(() => data.value?.capital || {})
const behavior = computed(() => data.value?.behavior || {})
const symbols = computed(() => data.value?.symbols || [])
const monthly = computed(() => data.value?.monthly || [])
const reviewPeriod = computed(() => {
  const meta = data.value?.meta
  if (!meta?.first_trade_at || !meta?.last_trade_at) return '暂无交易记录'
  return `${meta.first_trade_at.slice(0, 10).replaceAll('-', '.')} — ${meta.last_trade_at.slice(0, 10).replaceAll('-', '.')}`
})

const findings = computed(() => {
  const b = behavior.value
  const c = capital.value
  const worst = symbols.value.filter((s) => toNumber(s.realized) < 0).slice(0, 3)
  const worstText = worst.map((s) => s.symbol).join('、')
  const worstSum = worst.reduce((acc, s) => acc + Math.abs(toNumber(s.realized)), 0)
  const totalLoss = symbols.value
    .filter((s) => toNumber(s.realized) < 0)
    .reduce((acc, s) => acc + Math.abs(toNumber(s.realized)), 0)
  const share = totalLoss ? Math.round((worstSum / totalLoss) * 100) : 0

  return [
    {
      title: '逆势加仓',
      tag: '最大代价',
      tone: 'crit',
      body:
        `在价格低于持仓均价时继续买入，全账户共 ${b.addon_below_avg ?? 0} 次。` +
        '均价下移制造了“成本变便宜了”的错觉，实际是放大亏损敞口。',
      evidence: [
        ['XE', '29.36 → 29.20 → 20.37 → 21.92 → 18.88 → 16.83 买入，14.08 卖出'],
        ['RKLB', '112.9 → 117 → 122.7 → 113.8 → 106.3 → 108.1 买入，24.5 卖出'],
      ],
    },
    {
      title: '赢家跑得快，输家扛得久',
      tag: '结构性问题',
      tone: 'crit',
      body:
        `盈利周期平均持有 ${b.avg_win_days ?? 0} 天，亏损周期平均 ${b.avg_loss_days ?? 0} 天。` +
        '赚钱时急着兑现，亏钱时等着回本，这个不对称直接决定了账户的期望值为负。',
      evidence: [
        ['盈利周期', `${b.avg_win_days ?? 0} 天 / 平均 ${formatMoney(b.avg_win, 'USD', 0)}`],
        ['亏损周期', `${b.avg_loss_days ?? 0} 天 / 平均 ${formatMoney(b.avg_loss, 'USD', 0)}`],
      ],
    },
    {
      title: '同日做 T',
      tag: `成功率 ${b.t_win_rate ?? 0}%`,
      tone: 'info',
      body:
        `同日买卖形成 ${b.t_trades ?? 0} 次做 T，成功 ${b.t_wins ?? 0} 次，` +
        `失败 ${b.t_losses ?? 0} 次。成功率按当日买卖配对的价差盈亏统计。`,
      evidence: [
        ['成功 / 失败', `${b.t_wins ?? 0} 胜 / ${b.t_losses ?? 0} 负`],
        ['价差盈亏', `${formatMoney(b.t_pnl)}（未计手续费）`],
        ['统计口径', '每个“标的 + 交易日”计一次；配对数量取当日买入量与卖出量的较小值'],
      ],
    },
    {
      title: '亏损高度集中',
      tag: '可修复',
      tone: 'warn',
      body:
        `${worstText} 三只贡献了全部亏损的约 ${share}%，而且它们的共同点是同一个动作：` +
        '价格跌破成本之后继续加仓。',
      evidence: worst.map((s) => [
        s.symbol,
        `已实现 ${formatMoney(s.realized)} · 逆势加仓 ${s.addon_below_avg} 次`,
      ]),
    },
    {
      title: '仓位大小与确信度反着来',
      tag: '配置问题',
      tone: 'warn',
      body:
        '单笔最大的几笔全在波动最大的标的上，而持续赚钱的标的反而给得仓位最小。' +
        '这让研究收益被波动吃掉。',
      evidence: [
        ['周转', `${c.turnover_x ?? 0} 倍本金，手续费约 ${formatMoney(c.commissions, 'USD', 0)}`],
        ['已实现', `${formatMoney(c.realized)}（${formatPct(c.realized_pct, 2)} 对入金）`],
      ],
    },
    {
      title: '决策压缩在开盘后九十分钟',
      tag: '节奏问题',
      tone: 'info',
      body:
        '绝大多数成交集中在北京时间 21:30–23:30，也就是美股开盘后的九十分钟。' +
        '那是噪音最大、滑点最高、也最容易临时起意的时段。',
      evidence: [
        ['峰值月份', `交易 ${Math.max(...monthly.value.map((m) => m.trades), 0)} 笔`],
        ['建议', '只在 22:30–23:10 挂限价单，其余时间关掉软件'],
      ],
    },
  ]
})

function renderBars() {
  if (!barRef.value) return
  if (!barChart) barChart = echarts.init(barRef.value)
  const compact = window.innerWidth <= 560
  const months = monthly.value.map((m) => m.month.slice(5) + '月')
  const pnl = monthly.value.map((m) => toNumber(m.realized))

  barChart.setOption(
    {
      animationDuration: 300,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params) => {
          const i = params[0]?.dataIndex ?? 0
          const m = monthly.value[i] || {}
          return `${m.month}<br/>交易 ${m.trades} 笔<br/>已实现 ${
            toNumber(m.realized) >= 0 ? '+' : ''
          }${moneyAxisFormatter(m.realized)}`
        },
      },
      grid: { left: compact ? 4 : 8, right: compact ? 40 : 56, top: 20, bottom: 34 },
      xAxis: {
        type: 'category',
        data: months,
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
        axisLabel: { ...axisLabelStyle, formatter: moneyAxisFormatter },
      },
      series: [
        {
          name: '已实现盈亏',
          type: 'bar',
          barMaxWidth: 32,
          data: pnl.map((v) => ({
            value: v,
            itemStyle: {
              color: v >= 0 ? GREEN : RED,
              borderRadius: v >= 0 ? [6, 6, 0, 0] : [0, 0, 6, 6],
            },
          })),
          label: {
            show: true,
            position: 'top',
            fontSize: 10,
            color: '#787b86',
            formatter: (p) => (p.value === 0 ? '' : moneyAxisFormatter(p.value)),
          },
        },
      ],
    },
    true,
  )
}

function renderCounts() {
  if (!countRef.value) return
  if (!countChart) countChart = echarts.init(countRef.value)
  const compact = window.innerWidth <= 560
  const months = monthly.value.map((m) => m.month.slice(5) + '月')
  const counts = monthly.value.map((m) => m.trades)
  const peak = Math.max(...counts, 0)

  countChart.setOption(
    {
      animationDuration: 300,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params) => {
          const i = params[0]?.dataIndex ?? 0
          const m = monthly.value[i] || {}
          return `${m.month}<br/><b>${m.trades} 笔</b>`
        },
      },
      grid: { left: compact ? 4 : 8, right: compact ? 40 : 56, top: 20, bottom: 34 },
      xAxis: {
        type: 'category',
        data: months,
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
        axisLabel: { ...axisLabelStyle, formatter: (v) => String(v) },
      },
      series: [
        {
          name: '交易笔数',
          type: 'bar',
          barMaxWidth: 32,
          data: counts.map((v) => ({
            value: v,
            // One series, so the peak is the only thing worth emphasizing.
            itemStyle: {
              color: v === peak && peak > 0 ? '#4b6de0' : '#c9d4ee',
              borderRadius: [6, 6, 0, 0],
            },
          })),
          label: {
            show: true,
            position: 'top',
            fontSize: 10,
            color: '#787b86',
            formatter: (p) => (p.value ? String(p.value) : ''),
          },
        },
      ],
    },
    true,
  )
}

function renderSymbols() {
  if (!symbolRef.value) return
  if (!symbolChart) symbolChart = echarts.init(symbolRef.value)
  const compact = window.innerWidth <= 560
  const rows = [...symbols.value].reverse()
  const values = rows.map((r) => toNumber(r.realized))

  symbolChart.setOption(
    {
      animationDuration: 300,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params) => {
          const i = params[0]?.dataIndex ?? 0
          const r = rows[i] || {}
          return (
            `<b>${r.symbol}</b><br/>已实现 ${r.realized >= 0 ? '+' : ''}${moneyAxisFormatter(r.realized)}` +
            `<br/>投入 ${moneyAxisFormatter(r.cost)}<br/>收益率 ${formatPct(r.return_pct, 1)}` +
            (r.addon_below_avg ? `<br/>逆势加仓 ${r.addon_below_avg} 次` : '')
          )
        },
      },
      grid: { left: compact ? 60 : 72, right: compact ? 64 : 88, top: 8, bottom: 8 },
      xAxis: {
        type: 'value',
        splitLine: noSplitLine,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { show: false },
      },
      yAxis: {
        type: 'category',
        data: rows.map((r) => tickerOf(r.symbol)),
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { ...axisLabelStyle, fontSize: 10 },
      },
      series: [
        {
          type: 'bar',
          barMaxWidth: 12,
          data: values.map((v) => ({
            value: v,
            itemStyle: {
              color: v >= 0 ? GREEN : RED,
              borderRadius: v >= 0 ? [0, 3, 3, 0] : [3, 0, 0, 3],
            },
            label: {
              show: true,
              position: v >= 0 ? 'right' : 'left',
              fontSize: 10,
              color: '#787b86',
              formatter: () => (v >= 0 ? '+' : '') + moneyAxisFormatter(v),
            },
          })),
        },
      ],
    },
    true,
  )
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await fetchTradingReview()
    await nextTick()
    renderBars()
    renderCounts()
    renderSymbols()
  } catch (e) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function onResize() {
  barChart?.resize()
  countChart?.resize()
  symbolChart?.resize()
  renderBars()
  renderCounts()
  renderSymbols()
}

onMounted(() => {
  load()
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  barChart?.dispose()
  countChart?.dispose()
  symbolChart?.dispose()
  barChart = null
  countChart = null
  symbolChart = null
})
</script>

<template>
  <div class="review">
    <header class="review-header">
      <div>
        <p class="page-eyebrow">交易分析 / 交易复盘</p>
        <h1>交易复盘<span class="title-dot"></span></h1>
        <p class="page-description">从交易结果到行为习惯，让下一次决策更有依据。</p>
      </div>
      <div class="header-actions">
        <span v-if="data" class="review-period"><CalendarOutlined />{{ reviewPeriod }}</span>
        <a-button class="refresh-button" :loading="loading" @click="load">
          <template #icon><ReloadOutlined /></template>
          刷新复盘
        </a-button>
      </div>
    </header>
    <a-spin :spinning="loading">
      <a-alert v-if="error" type="error" :message="error" show-icon class="err" />
      <div v-if="loading && !data" class="loading-panel">
        <a-skeleton active :paragraph="{ rows: 5 }" />
      </div>

      <template v-if="data">
        <!-- headline -->
        <section class="hero">
          <div class="hero-main">
            <p class="eyebrow"><span class="hero-dot"></span>已实现盈亏</p>
            <p class="hero-num" :class="pnlClass(capital.realized)">
              {{ formatMoney(capital.realized, 'USD', 0).replace(' USD', '') }}<small>USD</small>
            </p>
            <p class="hero-sub">
              <span class="return-chip" :class="pnlClass(capital.realized_pct)">{{ formatPct(capital.realized_pct, 1) }}</span>
              相对 {{ formatMoney(capital.deposits, 'USD', 0) }} 累计入金
            </p>
            <div class="hero-meta"><span>{{ data.meta.trades }} 笔交易</span><span>{{ data.meta.symbols }} 个标的</span><span>{{ behavior.closed_cycles }} 个完整周期</span></div>
          </div>
          <div class="hero-side">
            <p class="hero-side-title">盈利与亏损的持仓对照</p>
            <div class="holding-comparison">
              <div><span class="comparison-label"><i class="comparison-dot win"></i>盈利周期</span><strong>{{ behavior.avg_win_days }}<small>天</small></strong><span class="comparison-money">平均盈利 {{ formatMoney(behavior.avg_win, 'USD', 0) }}</span></div>
              <div><span class="comparison-label"><i class="comparison-dot loss"></i>亏损周期</span><strong>{{ behavior.avg_loss_days }}<small>天</small></strong><span class="comparison-money">平均亏损 {{ formatMoney(behavior.avg_loss, 'USD', 0) }}</span></div>
            </div>
            <p class="hero-side-foot">对照持有时间与单次盈亏，观察交易中的不对称。</p>
          </div>
        </section>

        <!-- KPI -->
        <div class="kpis">
          <div class="kpi">
            <span class="k-label">胜率<AimOutlined class="k-icon blue" /></span>
            <span class="k-val">{{ behavior.win_rate }}%</span>
            <span class="k-sub">{{ behavior.wins }} 胜 / {{ behavior.closed_cycles }} 个完整周期</span>
          </div>
          <div class="kpi">
            <span class="k-label">盈亏比<PieChartOutlined class="k-icon violet" /></span>
            <span class="k-val" :class="behavior.payoff < 1 ? 'pnl-down' : 'pnl-up'">
              {{ behavior.payoff }}
            </span>
            <span class="k-sub">赢 {{ formatMoney(behavior.avg_win, 'USD', 0) }} : 输 {{ formatMoney(behavior.avg_loss, 'USD', 0) }}</span>
          </div>
          <div class="kpi">
            <span class="k-label">资金周转<SwapOutlined class="k-icon teal" /></span>
            <span class="k-val">{{ capital.turnover_x }}×</span>
            <span class="k-sub">手续费约 {{ formatMoney(capital.commissions, 'USD', 0) }}</span>
          </div>
          <div class="kpi">
            <span class="k-label">逆势加仓<SafetyOutlined class="k-icon amber" /></span>
            <span class="k-val" :class="behavior.addon_below_avg ? 'pnl-down' : 'pnl-up'">
              {{ behavior.addon_below_avg }} 次
            </span>
            <span class="k-sub">做 T {{ behavior.t_trades }} 次 · 成功率 {{ behavior.t_win_rate }}%</span>
          </div>
        </div>

        <nav class="review-nav" aria-label="复盘内容导航">
          <span class="nav-label">本页内容</span>
          <a href="#review-monthly">月度节奏</a>
          <a href="#review-findings">行为诊断</a>
          <router-link to="/strategy" class="nav-next">下一步：查看策略 <span>→</span></router-link>
        </nav>

        <!-- charts -->
        <h2 id="review-monthly" class="section-title"><span class="section-number">01</span>月度节奏<span class="section-badge">{{ monthly.length }} 个月</span></h2>
        <p class="section-sub">对照每月交易频率与已实现盈亏，观察交易节奏。</p>
        <div class="pair">
          <div class="chart-card">
            <div class="chart-heading"><p class="pair-label">每月已实现盈亏</p><span class="chart-unit">USD</span></div>
            <div class="chart-legend"><span><i class="legend-dot positive"></i>盈利</span><span><i class="legend-dot negative"></i>亏损</span></div>
            <div ref="barRef" class="chart-box"></div>
          </div>
          <div class="chart-card">
            <div class="chart-heading"><p class="pair-label">每月交易笔数</p><span class="chart-unit">笔</span></div>
            <div class="chart-legend"><span><i class="legend-dot peak"></i>峰值月份</span><span><i class="legend-dot normal"></i>其他月份</span></div>
            <div ref="countRef" class="chart-box"></div>
          </div>
        </div>

        <div class="chart-card symbol-card">
          <div class="chart-heading"><p class="pair-label">各标的已实现盈亏</p><span class="section-badge">{{ symbols.length }} 个标的</span></div>
          <p class="chart-description">左为亏损，右为盈利。悬停查看投入成本与收益率。</p>
          <div ref="symbolRef" class="chart-box tall" :style="{ height: `${Math.max(400, symbols.length * 25)}px` }"></div>
        </div>

        <!-- findings -->
        <h2 id="review-findings" class="section-title"><span class="section-number">02</span>行为诊断<span class="section-badge">{{ findings.length }} 个观察</span></h2>
        <p class="section-sub">把反复出现的行为放在一起，找到值得优先改进的习惯。</p>
        <div class="findings">
          <article v-for="(f, i) in findings" :key="f.title" class="finding" :class="f.tone">
            <span class="f-index">{{ String(i + 1).padStart(2, '0') }}</span>
            <div class="f-body">
              <h3 class="f-title">
                {{ f.title }}
                <span class="tag" :class="f.tone">{{ f.tag }}</span>
              </h3>
              <p class="f-text">{{ f.body }}</p>
              <div class="evidence">
                <div v-for="(line, j) in f.evidence" :key="j">
                  <b>{{ line[0] }}</b>
                  <span>{{ line[1] }}</span>
                </div>
              </div>
            </div>
          </article>
        </div>

        <p class="disclaimer">
          数据来自账本流水重放，口径与持股页一致：手续费不计入成本，卖出按加权均价结转。
          盈亏为已实现部分，不含浮动。
        </p>
      </template>
    </a-spin>
  </div>
</template>

<style scoped>
.review {
  --review-border: #e7ebf2;
  --review-ink: #243047;
  --review-muted: #7b8598;
  --review-blue: #4b6de0;
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  padding: 8px 0 32px;
  color: var(--review-ink);
}
.review-header { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 26px; }
.page-eyebrow { margin: 0 0 8px; font-size: 11px; font-weight: 600; letter-spacing: .06em; color: var(--review-muted); }
h1 { display: flex; align-items: center; gap: 10px; margin: 0; font-size: 27px; font-weight: 700; letter-spacing: -.04em; line-height: 1.3; }
.title-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--review-blue); }
.page-description { margin: 8px 0 0; font-size: 13px; color: var(--review-muted); line-height: 1.6; }
.header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 14px; }
.review-period { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; color: #667188; font-variant-numeric: tabular-nums; }
.refresh-button { height: 36px; border-radius: 9px; border-color: #dce3f4; color: #4662bf; background: #f9faff; font-size: 12px; box-shadow: none; }
.err { margin-bottom: 16px; }
.loading-panel { min-height: 300px; padding: 28px; border: 1px solid var(--review-border); border-radius: 18px; }
.hero { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); align-items: center; gap: 32px; padding: 30px 34px; margin-bottom: 18px; background: linear-gradient(115deg, #f6f8ff 0%, #fafbff 52%, #f0f5fc 100%); border: 1px solid #e1e7f5; border-radius: 20px; }
.hero-main { min-width: 0; }
.eyebrow { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; color: #66738b; margin: 0 0 14px; }
.hero-dot { height: 6px; width: 6px; background: #4b6de0; border-radius: 50%; }
.hero-num { font-size: clamp(32px, 4.5vw, 52px); font-weight: 700; line-height: 1.12; letter-spacing: -.045em; margin: 0; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.hero-num small { margin-left: 10px; font-size: 16px; font-weight: 500; letter-spacing: 0; color: #8791a3; }
.hero-sub { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; font-size: 12px; color: #77839a; margin: 16px 0 0; }
.return-chip { background: #fff; padding: 4px 9px; border: 1px solid #e6eaf2; border-radius: 6px; font-size: 12px; font-weight: 600; }
.hero-meta { display: flex; flex-wrap: wrap; gap: 10px 0; margin-top: 20px; font-size: 12px; color: #667188; }
.hero-meta span + span { border-left: 1px solid #d9e0ed; padding-left: 14px; margin-left: 14px; }
.hero-side { padding: 20px 24px; border: 1px solid rgba(222, 229, 242, .9); border-radius: 14px; background: rgba(255, 255, 255, .8); }
.hero-side-title { font-size: 12px; color: #53627d; font-weight: 600; margin: 0 0 18px; }
.holding-comparison { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.holding-comparison > div { display: flex; flex-direction: column; min-width: 0; }
.holding-comparison > div + div { border-left: 1px solid #e7ebf2; padding-left: 22px; }
.comparison-label { display: flex; align-items: center; gap: 7px; font-size: 11px; color: #7b8598; }
.comparison-dot { width: 6px; height: 6px; border-radius: 50%; }
.comparison-dot.win { background: #14977d; }
.comparison-dot.loss { background: #e35d6a; }
.holding-comparison strong { margin-top: 9px; font-size: 28px; line-height: 1.2; font-weight: 650; letter-spacing: -.03em; font-variant-numeric: tabular-nums; }
.holding-comparison small { font-size: 12px; color: #7b8598; font-weight: 400; margin-left: 6px; }
.comparison-money { font-size: 11px; color: #7b8598; margin-top: 7px; line-height: 1.5; }
.hero-side-foot { border-top: 1px solid #e7ebf2; padding-top: 12px; margin: 16px 0 0; font-size: 11px; color: #8791a3; line-height: 1.6; }
.kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.kpi { padding: 18px 20px; min-width: 0; border: 1px solid var(--review-border); border-radius: 14px; background: #fff; box-shadow: 0 3px 14px rgba(38, 58, 95, .025); }
.k-label { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 12px; color: #748097; margin-bottom: 9px; }
.k-icon { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 9px; font-size: 15px; }
.k-icon.blue { color: #4b6de0; background: #eef2ff; }
.k-icon.violet { color: #8a68c9; background: #f3effb; }
.k-icon.teal { color: #14977d; background: #eaf7f3; }
.k-icon.amber { color: #b78631; background: #fcf5e8; }
.k-val { display: block; font-size: 29px; font-weight: 650; line-height: 1.2; letter-spacing: -.035em; font-variant-numeric: tabular-nums; }
.k-sub { display: block; font-size: 11px; line-height: 1.6; color: #8791a3; margin-top: 10px; }
.review-nav { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin: 24px 0 4px; padding: 9px 12px; border: 1px solid #edf0f6; border-radius: 10px; background: #f8f9fc; font-size: 12px; }
.nav-label { color: #97a0b0; margin: 0 12px 0 4px; }
.review-nav a { color: #65728a; padding: 6px 10px; border-radius: 6px; transition: background .15s, color .15s; }
.review-nav a:hover { color: #4b6de0; background: #edf1ff; }
.review-nav .nav-next { margin-left: auto; color: #4863b6; font-weight: 600; }
.review-nav .nav-next span { margin-left: 4px; }
.section-title { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; font-size: 19px; font-weight: 650; letter-spacing: -.025em; margin: 34px 0 8px; scroll-margin-top: 200px; }
.section-number { font-size: 11px; font-weight: 600; color: #7085c3; background: #eef2fc; padding: 5px 6px; border-radius: 5px; font-variant-numeric: tabular-nums; letter-spacing: 0; }
.section-badge { display: inline-flex; align-items: center; font-size: 11px; color: #8992a4; background: #f3f5f9; border-radius: 5px; padding: 3px 8px; font-weight: 400; letter-spacing: 0; }
.section-sub { font-size: 12px; color: #7b8598; margin: 0 0 18px; line-height: 1.7; }
.pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.chart-card { min-width: 0; padding: 22px 22px 10px; border: 1px solid var(--review-border); border-radius: 16px; background: #fff; box-shadow: 0 3px 14px rgba(38, 58, 95, .025); }
.chart-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.pair-label { font-size: 13px; font-weight: 600; margin: 0; color: #37465e; }
.chart-unit { font-size: 10px; color: #96a0b2; }
.chart-legend { display: flex; align-items: center; gap: 16px; margin-top: 12px; font-size: 10px; color: #8791a3; }
.chart-legend span { display: flex; align-items: center; gap: 6px; }
.legend-dot { width: 6px; height: 6px; border-radius: 2px; }
.legend-dot.positive { background: #14977d; }
.legend-dot.negative { background: #e35d6a; }
.legend-dot.peak { background: #4b6de0; }
.legend-dot.normal { background: #c9d4ee; }
.chart-box { width: 100%; height: 270px; }
.symbol-card { margin-top: 18px; padding-bottom: 18px; }
.chart-description { margin: 10px 0 16px; font-size: 11px; color: #8992a4; line-height: 1.6; }
.findings { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.finding { --finding-accent: #d39a43; --finding-soft: #fcf5e8; display: flex; align-items: flex-start; gap: 14px; min-width: 0; padding: 22px; border: 1px solid var(--review-border); border-top: 3px solid var(--finding-accent); border-radius: 14px; background: #fff; }
.finding.crit { --finding-accent: #df8590; --finding-soft: #fcf0f2; }
.finding.info { --finding-accent: #8199dc; --finding-soft: #eef2ff; }
.f-index { display: grid; place-items: center; flex-shrink: 0; width: 32px; height: 32px; background: var(--finding-soft); color: var(--finding-accent); border-radius: 9px; font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
.f-body { display: flex; flex-direction: column; min-width: 0; height: 100%; }
.f-title { font-size: 14px; font-weight: 650; margin: 4px 0 12px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; line-height: 1.5; }
.tag { font-size: 10px; font-weight: 400; padding: 2px 7px; border-radius: 5px; white-space: nowrap; }
.tag.crit, .pill.cut { background: #fceff1; color: #ba5463; }
.tag.warn, .pill.trim { background: #fcf5e8; color: #a77a30; }
.tag.info { background: #eef2ff; color: #5876c2; }
.f-text { font-size: 12px; line-height: 1.85; color: #68758a; margin: 0 0 16px; }
.evidence { padding: 12px; background: #f8f9fc; border: 1px solid #edf0f5; border-radius: 8px; font-size: 11px; line-height: 1.8; color: #8590a3; margin-top: auto; overflow-wrap: anywhere; }
.evidence > div { display: grid; grid-template-columns: minmax(48px, auto) minmax(0, 1fr); gap: 10px; }
.evidence > div + div { margin-top: 5px; }
.evidence b { color: #55657e; font-weight: 600; }
.alloc { margin: 0 0 18px; padding: 22px 24px; border: 1px solid var(--review-border); border-radius: 14px; background: #fafbfe; }
.alloc-bar { display: flex; gap: 4px; height: 36px; border-radius: 8px; overflow: hidden; margin-top: 18px; }
.seg { display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; color: #fff; white-space: nowrap; overflow: hidden; }
.seg.core { flex: 6; background: #506db9; }
.seg.sat { flex: 3; background: #78a7bb; }
.seg.cash { flex: 1; background: #dce5ef; color: #617287; }
.alloc-legend { display: flex; flex-wrap: wrap; gap: 10px 24px; margin-top: 14px; font-size: 11px; color: #748097; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 2px; margin-right: 6px; }
.dot.core { background: #506db9; }
.dot.sat { background: #78a7bb; }
.dot.cash { background: #dce5ef; }
.strat-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.strat { border: 1px solid var(--review-border); border-radius: 14px; padding: 22px; display: flex; flex-direction: column; gap: 14px; min-width: 0; background: #fff; }
.norm { align-self: flex-start; font-size: 10px; font-weight: 600; color: #6480bd; background: #f0f3fb; border-radius: 5px; padding: 4px 8px; }
.strat h3 { margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -.02em; }
.strat ul, .strat ol { margin: 0 0 4px; padding-left: 17px; font-size: 12px; line-height: 1.85; color: #748097; }
.strat li { margin-bottom: 10px; padding-left: 2px; }
.strat li:last-child { margin-bottom: 0; }
.strat li::marker { color: #9bacc8; }
.strat li b { color: #4a5a72; font-weight: 600; }
.strat .foot { margin: auto 0 0; padding-top: 13px; border-top: 1px solid #edf0f5; font-size: 11px; color: #8a94a6; line-height: 1.75; }
.table-wrap { overflow-x: auto; border: 1px solid var(--review-border); border-radius: 14px; }
table { width: 100%; min-width: 650px; border-collapse: collapse; font-size: 12px; background: #fff; }
th { text-align: left; font-size: 11px; font-weight: 500; color: #8792a7; padding: 14px 20px; background: #f8f9fc; border-bottom: 1px solid var(--review-border); white-space: nowrap; }
td { padding: 16px 20px; border-bottom: 1px solid #edf0f5; color: #66758b; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: #fafbff; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.symbol-name { display: flex; align-items: center; gap: 10px; }
.ticker-avatar { display: grid; place-items: center; flex-shrink: 0; width: 30px; height: 30px; border: 1px solid #e6ebf5; border-radius: 9px; color: #6b82ba; background: #f3f6fc; font-size: 12px; font-weight: 650; }
.sym { color: #43536c; font-weight: 650; font-variant-numeric: tabular-nums; }
.pill { display: inline-block; font-size: 11px; padding: 4px 9px; border-radius: 5px; white-space: nowrap; }
.pill.hold { background: #eaf6f2; color: #388a74; }
.note { display: block; font-size: 10px; color: #98a1b0; margin-top: 6px; }
.empty { text-align: center; color: #8791a3; padding: 32px; }
.checklist { margin: 0; padding: 6px 24px; list-style: none; counter-reset: chk; border: 1px solid #e3e9f4; border-radius: 14px; background: #f8faff; }
.checklist li { counter-increment: chk; display: grid; grid-template-columns: 28px minmax(0, 1fr); align-items: start; gap: 14px; padding: 16px 0; border-bottom: 1px solid #e8edf6; font-size: 12px; line-height: 1.8; color: #748097; }
.checklist li:last-child { border-bottom: 0; }
.checklist li::before { content: counter(chk, decimal-leading-zero); display: grid; place-items: center; width: 25px; height: 25px; border: 1px solid #dce4f4; border-radius: 7px; font-size: 10px; font-weight: 600; color: #7590c7; background: #fff; }
.checklist b { color: #4a5c7b; font-weight: 600; }
.disclaimer { font-size: 11px; color: #96a0b2; line-height: 1.8; margin: 22px 0 0; padding: 0 2px; }
@media (max-width: 1100px) {
  .hero { padding: 26px; gap: 24px; }
  .kpis { gap: 12px; }
  .kpi { padding: 16px; }
  .finding { padding: 18px; gap: 10px; }
  .strat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 800px) {
  .review { padding-top: 2px; }
  .review-header { align-items: flex-start; }
  .header-actions { flex-direction: column; align-items: flex-end; gap: 10px; }
  .review-period { font-size: 10px; }
  .hero { grid-template-columns: 1fr; gap: 24px; }
  .hero-num { font-size: 44px; }
  .hero-side { padding: 20px; }
  .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .pair, .findings { grid-template-columns: 1fr; }
  .section-title { scroll-margin-top: 24px; }
  .chart-card { padding: 20px 18px 10px; }
  .f-body { flex: 1; }
  .evidence { margin-top: 0; }
}
@media (max-width: 560px) {
  .review-header { flex-direction: column; gap: 16px; margin-bottom: 20px; }
  h1 { font-size: 24px; }
  .page-description { font-size: 12px; }
  .header-actions { width: 100%; flex-direction: row; align-items: center; justify-content: space-between; }
  .hero { padding: 22px 18px; border-radius: 16px; gap: 20px; }
  .hero-num { font-size: clamp(30px, 8.7vw, 42px); }
  .hero-sub { gap: 8px; font-size: 11px; }
  .hero-meta { font-size: 10px; margin-top: 16px; }
  .hero-meta span + span { padding-left: 10px; margin-left: 10px; }
  .hero-side { padding: 16px; border-radius: 10px; }
  .holding-comparison { gap: 12px; }
  .holding-comparison > div + div { padding-left: 14px; }
  .holding-comparison strong { font-size: 26px; }
  .comparison-money { font-size: 10px; }
  .kpis { gap: 10px; }
  .kpi { padding: 14px; border-radius: 12px; }
  .k-label { font-size: 11px; }
  .k-icon { width: 26px; height: 26px; font-size: 13px; }
  .k-val { font-size: 25px; }
  .k-sub { font-size: 10px; }
  .review-nav { margin-top: 18px; gap: 4px; padding: 8px; font-size: 11px; }
  .review-nav .nav-next { margin-left: 0; }
  .nav-label { display: none; }
  .review-nav a { flex: 1; text-align: center; padding: 6px 4px; white-space: nowrap; }
  .section-title { margin-top: 28px; font-size: 17px; gap: 8px; }
  .section-sub { font-size: 11px; }
  .chart-card { padding: 18px 12px 8px; border-radius: 12px; }
  .chart-box { height: 240px; }
  .finding { padding: 16px; border-radius: 12px; }
  .f-index { width: 27px; height: 27px; font-size: 10px; }
  .f-title { font-size: 13px; margin-top: 2px; }
  .f-text { font-size: 12px; }
  .evidence > div { grid-template-columns: 1fr; gap: 1px; }
  .evidence > div + div { margin-top: 8px; }
  .alloc { padding: 18px 16px; }
  .alloc-bar { height: 32px; }
  .alloc-legend { gap: 8px; font-size: 10px; }
  .strat-grid { grid-template-columns: 1fr; gap: 12px; }
  .strat { padding: 20px; border-radius: 12px; }
  th, td { padding: 12px 14px; }
  .checklist { padding: 4px 16px; }
  .checklist li { gap: 10px; font-size: 12px; }
}
</style>


