<script setup>
import { computed } from 'vue'
import { tickerOf } from '@/utils/format'

const props = defineProps({
  symbol: String,
  name: String,
  logoUrl: String,
  asButton: Boolean,
})

defineEmits(['pick'])

const ticker = computed(() => tickerOf(props.symbol))
const listed = computed(() => /^(SSE|SZSE|BSE|HKEX):/.test(String(props.symbol || '').toUpperCase()))
const chineseName = computed(() => {
  const name = String(props.name || '').trim()
  if (!listed.value || !name || name === ticker.value || name === props.symbol) return ''
  return name
})
const primary = computed(() => chineseName.value || ticker.value)
const secondary = computed(() => {
  if (chineseName.value) return ticker.value
  const name = String(props.name || '').trim()
  if (!name || name === ticker.value || name === props.symbol) return ''
  return name
})

// TradingView's primary listing exchange can differ from the exchange
// prefix used by the ledger/quote provider (especially NYSE Arca ETFs).
// Keep the ledger symbol unchanged and only override the detail-page route.
const TRADINGVIEW_EXCHANGE_BY_TICKER = Object.freeze({
  COHR: 'NYSE',
  HIMX: 'NASDAQ',
  NASA: 'AMEX',
  EWT: 'AMEX',
  CRCA: 'AMEX',
  CRCD: 'CBOE',
  NRIX: 'NASDAQ',
  OKLS: 'AMEX',
  PSQ: 'AMEX',
  QID: 'AMEX',
  SOXS: 'AMEX',
  SSG: 'AMEX',
  RKLX: 'NASDAQ',
  RKLZ: 'NASDAQ',
  TSLQ: 'NASDAQ',
  SPCX: 'NASDAQ',
  XE: 'NASDAQ',
})

const href = computed(() => {
  const raw = String(props.symbol || '').trim().toUpperCase()
  if (!raw || ['CASH', 'USD'].includes(raw)) return null
  const exchange =
    TRADINGVIEW_EXCHANGE_BY_TICKER[ticker.value] || (raw.includes(':') ? raw.split(':', 1)[0] : null)
  const tvSymbol = exchange ? `${exchange}-${ticker.value}` : ticker.value
  if (!tvSymbol || ['CASH', 'USD'].includes(tvSymbol)) return null
  return `https://cn.tradingview.com/symbols/${encodeURIComponent(tvSymbol)}/`
})
</script>

<template>
  <button
    v-if="asButton"
    type="button"
    class="symbol-cell symbol-cell--button"
    @click.stop="$emit('pick')"
  >
    <div class="symbol-logo">
      <img v-if="logoUrl" :src="logoUrl" :alt="symbol" />
      <span v-else>{{ primary.slice(0, 2) }}</span>
    </div>
    <div class="symbol-meta">
      <div class="code">{{ primary }}</div>
      <div v-if="secondary" class="name">{{ secondary }}</div>
    </div>
  </button>
  <a
    v-else-if="href"
    class="symbol-cell symbol-cell--link"
    :href="href"
    target="_blank"
    rel="noopener noreferrer"
    @click.stop
  >
    <div class="symbol-logo">
      <img v-if="logoUrl" :src="logoUrl" :alt="symbol" />
      <span v-else>{{ primary.slice(0, 2) }}</span>
    </div>
    <div class="symbol-meta">
      <div class="code">{{ primary }}</div>
      <div v-if="secondary" class="name">{{ secondary }}</div>
    </div>
  </a>
  <div v-else class="symbol-cell">
    <div class="symbol-logo">
      <img v-if="logoUrl" :src="logoUrl" :alt="symbol" />
      <span v-else>{{ primary.slice(0, 2) }}</span>
    </div>
    <div class="symbol-meta">
      <div class="code">{{ primary }}</div>
      <div v-if="secondary" class="name">{{ secondary }}</div>
    </div>
  </div>
</template>
