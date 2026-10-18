<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  AimOutlined,
  ArrowRightOutlined,
  CalendarOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  PieChartOutlined,
  ReloadOutlined,
  SafetyOutlined,
} from '@ant-design/icons-vue'
import { fetchTradingStrategy } from '@/api/portfolio'
import { formatMoney, formatPct, pnlClass, tickerOf } from '@/utils/format'

const data = ref(null)
const loading = ref(true)
const error = ref('')
const candidateFilter = ref('ready')

const capital = computed(() => data.value?.capital || {})
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
  openPositions.value.reduce((sum, p) => sum + Number(p.pnl || 0), 0),
)
const positionCost = computed(() =>
  openPositions.value.reduce((sum, p) => sum + Number(p.cost || 0), 0),
)
const positionPnlPct = computed(() =>
  positionCost.value > 0 ? (positionPnl.value / positionCost.value) * 100 : 0,
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
const reviewPeriod = computed(() => {
  const meta = data.value?.meta
  if (!meta?.first_trade_at || !meta?.last_trade_at) return '暂无交易记录'
  return `${meta.first_trade_at.slice(0, 10).replaceAll('-', '.')} — ${meta.last_trade_at.slice(0, 10).replaceAll('-', '.')}`
})

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
    const result = await fetchTradingStrategy()
    data.value = result
    if (candidateFilter.value === 'ready' && !result.reentry_candidates?.some((c) => c.status === 'ready')) {
      candidateFilter.value = 'all'
    }
  } catch (e) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
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
        <span v-if="data" class="period-label"><CalendarOutlined />{{ reviewPeriod }}</span>
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
              按 {{ formatMoney(capital.deposits, 'USD', 0) }} 本金将资金分成核心、卫星与现金三层，
              用固定仓位、止损和交易窗口约束每一次决策。
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
              <div><p>目标资金结构</p><strong>60<small> / 30 / 10</small></strong><span>核心 / 卫星 / 现金</span></div>
            </article>
            <article class="hero-metric">
              <span class="metric-icon teal"><AimOutlined /></span>
              <div><p>单只仓位上限</p><strong>{{ formatMoney(capital.deposits * 0.08, 'USD', 0) }}</strong><span>占账户本金 8%</span></div>
            </article>
            <article class="hero-metric">
              <span class="metric-icon amber"><SafetyOutlined /></span>
              <div><p>单笔硬止损</p><strong>−12<small>%</small></strong><span>1R = 入场价 × 12%</span></div>
            </article>
            <article class="hero-metric">
              <span class="metric-icon violet"><AimOutlined /></span>
              <div><p>盈亏比目标</p><strong>{{ behavior.payoff ?? '—' }}<small> → 1.5</small></strong><span>平均盈利 ÷ 平均亏损</span></div>
            </article>
          </div>
        </section>

        <nav class="strategy-nav" aria-label="策略内容导航">
          <span class="nav-label">执行手册</span>
          <a href="#strategy-allocation">资金配置</a>
          <a href="#strategy-rules">交易纪律</a>
          <a href="#strategy-positions">持仓动作</a>
          <a href="#strategy-reentry">再买入观察</a>
          <a href="#strategy-checklist">下单清单</a>
          <router-link to="/review" class="nav-next">回看交易数据 <span>→</span></router-link>
        </nav>

        <h2 id="strategy-allocation" class="section-title"><span class="section-number">01</span>资金配置<span class="section-badge">总仓位 100%</span></h2>
        <p class="section-sub">先决定每类资金做什么，再决定具体买什么。</p>

        <div class="alloc-card">
          <div class="alloc-heading">
            <div>
              <p>目标资金配置</p>
              <span>按 {{ formatMoney(capital.deposits, 'USD', 0) }} 本金设定，季度检查一次偏离度。</span>
            </div>
            <strong>100<small>%</small></strong>
          </div>
          <div class="alloc-bar" aria-label="核心 60%，卫星 30%，现金 10%">
            <div class="seg core"><span>核心 60%</span></div>
            <div class="seg sat"><span>卫星 30%</span></div>
            <div class="seg cash"><span>10%</span></div>
          </div>
          <div class="alloc-list">
            <article class="alloc-item core">
              <span class="alloc-index">01</span>
              <div><p>核心仓 <strong>60%</strong></p><span>2–3 只宽基或龙头，按月定投，偏离目标 20% 才再平衡。</span></div>
            </article>
            <article class="alloc-item sat">
              <span class="alloc-index">02</span>
              <div><p>卫星仓 <strong>30%</strong></p><span>最多 5 只，单只上限 {{ formatMoney(capital.deposits * 0.08, 'USD', 0) }}，分三批建仓。</span></div>
            </article>
            <article class="alloc-item cash">
              <span class="alloc-index">03</span>
              <div><p>现金 <strong>10%</strong></p><span>预留弹药，用完即停手，不卖出持仓来凑钱。</span></div>
            </article>
          </div>
        </div>

        <h2 id="strategy-rules" class="section-title"><span class="section-number">02</span>交易纪律<span class="section-badge">A–F 六条规则</span></h2>
        <p class="section-sub">每条规则都对应复盘中的具体问题，执行优先于解释。</p>

        <div class="strat-grid">
          <article class="strat">
            <span class="norm">A · 仓位</span>
            <h3>资金分层</h3>
            <ul>
              <li><b>核心 60%</b>：2–3 只宽基或龙头，按月定投，不择时、不主动止盈，偏离目标 20% 才再平衡。</li>
              <li><b>卫星 30%</b>：最多 5 只，单只上限 {{ formatMoney(capital.deposits * 0.08, 'USD', 0) }}，单笔新建仓 900–1,200，分三批。</li>
              <li><b>现金 10%</b>：用完即停手，不允许卖出持仓来凑钱。</li>
            </ul>
            <p class="foot">单笔最大亏损 = 1,200 × 12% ≈ 145，即账户 0.5%。</p>
          </article>

          <article class="strat">
            <span class="norm">B · 建仓</span>
            <h3>只在盈利时加仓</h3>
            <ol>
              <li>下单前在备注写一句买入理由，写不出来就不买。</li>
              <li>分三批：1/3 试仓 → 站稳再加 → 突破再加。<b>只有价格高于成本才允许加后两批。</b></li>
              <li>价格低于成本时，<b>最多补一次</b>，补完不超过计划仓位。</li>
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
              <li><b>止损 −12%</b>，或买入逻辑被破坏，触发即走。</li>
              <li><b>1.5R 卖 1/3，2.5R 再卖 1/3</b>，剩 1/3 用 8% 建议止损让它跑。</li>
              <li>持仓 <b>40 天</b>且没有新逻辑，月度复盘时清掉。</li>
              <li>禁止同日对同一标的一买一卖；禁止多单与反向 ETF 并存。</li>
            </ol>
            <div class="risk-guide">
              <div class="risk-definition">
                <span class="risk-symbol">R</span>
                <div>
                  <b>初始风险单位</b>
                  <span>R = 入场价 − 止损价；按 −12% 止损时，R = 入场价 × 12%</span>
                </div>
              </div>
              <div class="risk-levels" aria-label="止盈止损价格档位">
                <div class="risk-level stop">
                  <span>−1R</span>
                  <b>−12%</b>
                  <small>全部止损</small>
                </div>
                <div class="risk-level take">
                  <span>+1.5R</span>
                  <b>+18%</b>
                  <small>卖出 1/3</small>
                </div>
                <div class="risk-level take">
                  <span>+2.5R</span>
                  <b>+30%</b>
                  <small>再卖 1/3</small>
                </div>
              </div>
              <div class="risk-follow">
                <span><b>最后 1/3</b><em>最高价回撤 8% 卖出</em></span>
                <span><b>时间止损</b><em>40 天无新逻辑清仓</em></span>
              </div>
              <p class="risk-example">
                以 $100 入场为例：止损 $88；涨到 $118 卖 1/3；涨到 $130 再卖 1/3，剩余仓位跟随 8% 建议止损。
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
              <li><b>每季</b>：检查核心仓偏离度，执行再平衡。</li>
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
        <p class="section-sub">行情和止损位由当前周期的收盘价计算；红色动作为规则已触发，需要人工确认并记录卖出。</p>

        <div class="execution-overview">
          <div class="execution-stat">
            <span>持仓市值</span>
            <strong>{{ formatMoney(positionValue, 'USD', 2) }}</strong>
            <small>成本 {{ formatMoney(positionCost, 'USD', 2) }}</small>
          </div>
          <div class="execution-stat">
            <span>浮动盈亏</span>
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
                <span>浮动盈亏</span>
                <strong :class="pnlClass(p.pnl)">
                  {{ formatMoney(p.pnl, 'USD', 2) }}
                  <em>{{ formatPct(p.pnl_pct) }}</em>
                </strong>
              </div>
            </div>

            <div class="position-plan">
              <div :class="{ 'suggestion-stop': suggestionStopHit(p) }">
                <span>建议止损</span>
                <strong>{{ priceText(p.trailing_stop_price) }}</strong>
                <small>高点 {{ priceText(p.high_water_price) }} × 92%</small>
              </div>
              <div :class="{ 'mandatory-stop': mandatoryStopHit(p) }">
                <span>必须止损</span>
                <strong>{{ priceText(p.hard_stop_price) }}</strong>
                <small>成本 × 88% · −12%</small>
              </div>
              <div>
                <span>已持有</span>
                <strong>{{ p.holding_days }} 天</strong>
                <small :class="{ due: p.time_stop_due }">{{ p.time_stop_due ? '已到 40 天复核线' : '40 天触发时间复核' }}</small>
              </div>
            </div>

            <p class="position-action-note" :class="{ 'suggestion-exit-note': p.action_tone === 'warning' }">{{ p.action_note }}</p>
            <div class="position-card-foot">
              <span v-if="p.action === 'exit'">跌破建议止损即全部清仓；低于成本 −12% 的必须止损位绝不能继续持有。</span>
              <span v-else-if="p.action === 'trim'">已到 40 天复核线：没有新逻辑就清仓；保留则不加仓。</span>
              <span v-else>建议止损低于成本 × 88% 时，改按必须止损执行；止损位只随新高上移。</span>
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
        <p class="section-sub">筛选近 180 天已清仓标的，优先显示满足企稳或突破条件的股票；这是待复核观察名单，不是自动买入。</p>

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
              <div><span>当前价</span><b>{{ priceText(c.current_price) }}</b></div>
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
              <span><b>下一步：人工确认</b>写下新买入理由，检查仓位不超过 8%，再下 1/3 试仓单。</span>
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
          <li><span>止损价是多少？<b>按 −12% 算，亏多少美元，占账户多少？</b></span></li>
          <li><span>这个标的是不是已经有仓位，或持有反向 / 杠杆 ETF？</span></li>
          <li><span>今天下了几笔？现在是不是 22:30–23:10 之外？</span></li>
        </ol>

        <p class="disclaimer">策略根据当前账户与复盘数据制定。仓位数字对应本金与现有规则；执行前仍需核对实际可用资金与持仓。</p>
      </template>
    </a-spin>
  </div>
</template>

<style scoped>
.strategy-page {
  --page-border: #e7ebf2;
  --page-ink: #243047;
  --page-muted: #7b8598;
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
.period-label { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; color: #667188; font-variant-numeric: tabular-nums; }
.refresh-button { height: 36px; border-radius: 9px; border-color: #dce3f4; color: #4662bf; background: #f9faff; font-size: 12px; box-shadow: none; }
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
.hero-metric strong { display: block; font-size: 21px; line-height: 1.22; letter-spacing: -.035em; font-variant-numeric: tabular-nums; color: #31445b; white-space: nowrap; }
.hero-metric strong small { font-size: 11px; font-weight: 500; letter-spacing: 0; color: #8490a2; }
.hero-metric > div > span { display: block; margin-top: 6px; font-size: 9px; line-height: 1.5; color: #929baa; }
.strategy-nav { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin: 24px 0 4px; padding: 9px 12px; border: 1px solid #edf0f6; border-radius: 10px; background: #f8f9fc; font-size: 12px; }
.nav-label { margin: 0 12px 0 4px; color: #97a0b0; }
.strategy-nav a { padding: 6px 10px; border-radius: 6px; color: #65728a; transition: background .15s, color .15s; }
.strategy-nav a:hover { color: #2d7564; background: #eaf6f2; }
.strategy-nav .nav-next { margin-left: auto; color: #4863b6; font-weight: 600; }
.strategy-nav .nav-next span { margin-left: 4px; }
.section-title { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 34px 0 8px; font-size: 19px; font-weight: 650; letter-spacing: -.025em; scroll-margin-top: 200px; }
.section-number { padding: 5px 6px; border-radius: 5px; background: #eaf5f1; color: #3c8a77; font-size: 11px; font-weight: 600; letter-spacing: 0; font-variant-numeric: tabular-nums; }
.section-badge { display: inline-flex; align-items: center; padding: 3px 8px; border-radius: 5px; background: #f3f5f9; color: #8992a4; font-size: 11px; font-weight: 400; letter-spacing: 0; }
.section-sub { margin: 0 0 18px; font-size: 12px; line-height: 1.7; color: #7b8598; }
.alloc-card { padding: 24px; border: 1px solid #dfe8e4; border-radius: 16px; background: linear-gradient(180deg, #fbfdfc, #f7fafb); }
.alloc-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.alloc-heading p { margin: 0 0 7px; font-size: 14px; font-weight: 650; color: #354c5e; }
.alloc-heading span { font-size: 11px; line-height: 1.6; color: #8490a0; }
.alloc-heading > strong { color: #296e60; font-size: 26px; line-height: 1; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
.alloc-heading > strong small { margin-left: 2px; font-size: 11px; font-weight: 500; color: #8797a2; }
.alloc-bar { display: flex; gap: 4px; height: 38px; margin-top: 22px; overflow: hidden; border-radius: 9px; }
.seg { display: flex; align-items: center; justify-content: center; min-width: 0; overflow: hidden; color: #fff; font-size: 11px; font-weight: 600; white-space: nowrap; }
.seg.core { flex: 6; background: linear-gradient(90deg, #3d6b61, #4d7c70); }
.seg.sat { flex: 3; background: linear-gradient(90deg, #6a9aaa, #86adba); }
.seg.cash { flex: 1; background: #dfe9e7; color: #5d716f; }
.alloc-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
.alloc-item { display: flex; align-items: flex-start; gap: 11px; min-width: 0; padding: 15px; border: 1px solid #e5ece9; border-radius: 11px; background: rgba(255, 255, 255, .72); }
.alloc-index { display: grid; place-items: center; flex: 0 0 auto; width: 25px; height: 25px; border-radius: 7px; background: #edf3f1; color: #598074; font-size: 9px; font-weight: 650; font-variant-numeric: tabular-nums; }
.alloc-item p { margin: 0 0 6px; font-size: 12px; font-weight: 650; color: #425969; }
.alloc-item p strong { color: #2f7565; font-weight: 700; }
.alloc-item > div > span { display: block; font-size: 10px; line-height: 1.7; color: #8994a1; }
.strat-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.strat { position: relative; display: flex; flex-direction: column; gap: 14px; min-width: 0; padding: 22px; overflow: hidden; border: 1px solid var(--page-border); border-radius: 14px; background: #fff; box-shadow: 0 4px 16px rgba(38, 58, 95, .025); transition: transform .18s ease, box-shadow .18s ease; }
.strat:hover { transform: translateY(-2px); box-shadow: 0 9px 24px rgba(38, 58, 95, .07); }
.strat::before { content: ''; position: absolute; top: 0; left: 22px; width: 32px; height: 2px; border-radius: 0 0 4px 4px; background: #6b9e91; }
.norm { align-self: flex-start; padding: 4px 8px; border-radius: 5px; background: #edf5f2; color: #4e8276; font-size: 10px; font-weight: 600; }
.strat h3 { margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -.02em; }
.strat ul, .strat ol { margin: 0 0 4px; padding-left: 17px; font-size: 12px; line-height: 1.85; color: #748097; }
.strat li { margin-bottom: 10px; padding-left: 2px; }
.strat li:last-child { margin-bottom: 0; }
.strat li::marker { color: #91aeaa; }
.strat li b { color: #4a5a72; font-weight: 600; }
.risk-guide { display: flex; flex-direction: column; gap: 10px; padding: 13px; border: 1px solid #dce9e4; border-radius: 10px; background: linear-gradient(145deg, #f3f9f6, #f9fbfe); }
.risk-definition { display: flex; align-items: center; gap: 10px; }
.risk-symbol { display: grid; place-items: center; flex: 0 0 auto; width: 32px; height: 32px; border-radius: 8px; background: #3f8b78; color: #fff; font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; box-shadow: 0 4px 10px rgba(63, 139, 120, .18); }
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
.strat .foot { margin: auto 0 0; padding-top: 13px; border-top: 1px solid #edf0f5; font-size: 11px; line-height: 1.75; color: #8a94a6; }
.strat .foot a { color: #4c75a0; font-weight: 600; }
.table-wrap { overflow-x: auto; border: 1px solid var(--page-border); border-radius: 14px; background: #fff; }
table { width: 100%; min-width: 650px; border-collapse: collapse; background: #fff; font-size: 12px; }
th { padding: 14px 20px; border-bottom: 1px solid var(--page-border); background: #f8f9fc; color: #8792a7; font-size: 11px; font-weight: 500; text-align: left; white-space: nowrap; }
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
.empty { padding: 32px; color: #8791a3; text-align: center; }
.checklist { margin: 0; padding: 6px 24px; border: 1px solid #dfe8e4; border-radius: 14px; background: #f8fbfa; list-style: none; counter-reset: chk; }
.checklist li { display: grid; grid-template-columns: 28px minmax(0, 1fr); align-items: start; gap: 14px; padding: 16px 0; border-bottom: 1px solid #e7eeeb; color: #748097; font-size: 12px; line-height: 1.8; counter-increment: chk; }
.checklist li:last-child { border-bottom: 0; }
.checklist li::before { content: counter(chk, decimal-leading-zero); display: grid; place-items: center; width: 25px; height: 25px; border: 1px solid #d8e6e1; border-radius: 7px; background: #fff; color: #628d81; font-size: 10px; font-weight: 600; }
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
  border: 1px solid #e5eaf2;
  border-radius: 16px;
  background: #fff;
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
  color: #fff;
  font-size: 10px;
  font-weight: 650;
  box-shadow: 0 4px 10px rgba(184, 90, 86, .16);
  transition: background .15s ease, transform .15s ease;
}
.clear-action:hover { color: #fff; background: #a84d49; transform: translateX(1px); }
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
.candidate-filter button:hover { color: #4c5f78; background: #fff; }
.candidate-filter button.active {
  background: #fff;
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
  border: 1px solid #e7ebf2;
  border-radius: 15px;
  background: #fff;
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
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 7px;
  margin-top: 10px;
}
.candidate-metrics > div { min-width: 0; padding: 8px 9px; border-radius: 8px; background: #f8fafc; }
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
  .period-label { font-size: 10px; }
  .strategy-hero { padding: 24px; }
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
</style>
