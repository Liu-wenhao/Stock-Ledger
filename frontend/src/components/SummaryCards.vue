<script setup>
import { computed } from 'vue'
import { formatPct, pnlClass, toNumber } from '@/utils/format'

const props = defineProps({
  summary: { type: Object, default: null },
  loading: Boolean,
})

function moneyParts(v, currency = 'USD') {
  const n = toNumber(v)
  const abs = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  return {
    amount: `${n < 0 ? '-' : ''}${abs}`,
    currency,
  }
}

function signedMoneyParts(v, currency = 'USD') {
  const n = toNumber(v)
  const parts = moneyParts(n, currency)
  if (n > 0) parts.amount = `+${parts.amount}`
  return parts
}

const cards = computed(() => {
  const s = props.summary
  if (!s) {
    return [
      { label: '投资组合金额', parts: null, secondaryLines: null },
      { label: '持仓盈亏', parts: null, secondaryLines: null },
      { label: '累计盈亏', parts: null, secondaryLines: null },
      { label: '清仓盈亏', parts: null, secondaryLines: null },
      { label: '手续费', parts: null, secondaryLines: null },
    ]
  }

  const holdingsValue = s.holdings_value ?? s.portfolio_value
  const portfolio = moneyParts(holdingsValue, s.currency)
  const cash = moneyParts(s.cash, s.currency)
  const unrealized = moneyParts(s.unrealized_pnl, s.currency)
  const dayChange = signedMoneyParts(s.day_change, s.currency)
  const realized = moneyParts(s.realized_pnl, s.currency)
  const dividends = moneyParts(s.dividends, s.currency)
  const commissions = moneyParts(s.commissions ?? 0, s.currency)
  const total = moneyParts(s.total_pnl, s.currency)

  return [
    {
      label: '投资组合金额',
      valueClass: pnlClass(holdingsValue),
      parts: {
        amount: portfolio.amount,
        currency: portfolio.currency,
      },
      secondaryLines: [
        {
          prefix: '现金',
          amount: cash.amount,
          currency: cash.currency,
        },
      ],
    },
    {
      label: '持仓盈亏',
      valueClass: pnlClass(s.unrealized_pnl),
      parts: {
        amount: unrealized.amount,
        currency: unrealized.currency,
        percent: formatPct(s.unrealized_pnl_percent),
      },
      secondaryLines: [
        {
          className: pnlClass(s.day_change),
          prefix: '较前一天',
          amount: dayChange.amount,
          percent: formatPct(s.day_change_percent),
        },
      ],
    },
    {
      label: '累计盈亏',
      valueClass: pnlClass(s.total_pnl),
      parts: {
        amount: total.amount,
        currency: total.currency,
        percent: formatPct(s.total_pnl_percent),
      },
      secondaryLines: [
        {
          className: pnlClass(s.annualized_return),
          prefix: '年化收益率',
          percent: formatPct(s.annualized_return),
        },
      ],
    },
    {
      label: '清仓盈亏',
      valueClass: pnlClass(s.realized_pnl),
      parts: {
        amount: realized.amount,
        currency: realized.currency,
      },
      secondaryLines: [
        {
          prefix: '股息',
          amount: dividends.amount,
          currency: dividends.currency,
        },
      ],
    },
    {
      label: '手续费',
      valueClass: '',
      parts: {
        amount: commissions.amount,
        currency: commissions.currency,
      },
      secondaryLines: [
        {
          prefix: '累计交易手续费',
        },
      ],
    },
  ]
})
</script>

<template>
  <div class="kpi-grid">
    <div v-for="card in cards" :key="card.label" class="kpi-card">
      <div class="label">{{ card.label }}</div>
      <a-skeleton v-if="loading && !summary" active :paragraph="false" />
      <template v-else-if="!card.parts">
        <div class="value">—</div>
        <div class="secondary">—</div>
      </template>
      <template v-else>
        <div class="value" :class="card.valueClass">
          <span class="amount">{{ card.parts.amount }}</span>
          <span v-if="card.parts.currency" class="currency">{{ card.parts.currency }}</span>
          <span v-if="card.parts.percent" class="percent">{{ card.parts.percent }}</span>
        </div>
        <div
          v-for="(line, idx) in card.secondaryLines || []"
          :key="idx"
          class="secondary"
          :class="line.className"
        >
          <span v-if="line.prefix" class="prefix">{{ line.prefix }}</span>
          <span v-if="line.amount" class="amount">{{ line.amount }}</span>
          <span v-if="line.currency" class="currency">{{ line.currency }}</span>
          <span v-if="line.percent" class="percent">{{ line.percent }}</span>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.kpi-card {
  background: #fff;
  border: 1px solid #e0e3eb;
  border-radius: 8px;
  padding: 16px 18px;
  min-height: 108px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
}

.label {
  font-size: 12px;
  color: #787b86;
  margin-bottom: 8px;
}

.value {
  font-size: 20px;
  font-weight: 700;
  line-height: 1.25;
  letter-spacing: -0.02em;
  color: #131722;
}

.value .currency,
.secondary .currency {
  margin-left: 4px;
  font-size: 12px;
  font-weight: 600;
}

.value .percent {
  margin-left: 8px;
  font-size: 16px;
  font-weight: 700;
}

.secondary {
  margin-top: 8px;
  font-size: 12px;
  color: #787b86;
}

.secondary .prefix {
  margin-right: 4px;
}

.secondary .amount {
  font-size: 12px;
}

.secondary .percent {
  margin-left: 6px;
  font-size: 12px;
  font-weight: 600;
}

@media (max-width: 1200px) {
  .kpi-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 800px) {
  .kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .kpi-grid {
    grid-template-columns: 1fr;
  }
}
</style>
