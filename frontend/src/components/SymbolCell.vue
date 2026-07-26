<script setup>
import { computed } from 'vue'
import { tickerOf } from '@/utils/format'

const props = defineProps({
  symbol: String,
  name: String,
  logoUrl: String,
})

const ticker = computed(() => tickerOf(props.symbol))

const href = computed(() => {
  const raw = String(props.symbol || '').trim().toUpperCase()
  if (!raw || ['CASH', 'USD'].includes(raw)) return null
  // NYSE:CRCD -> https://cn.tradingview.com/symbols/NYSE-CRCD/
  const tvSymbol = raw.includes(':') ? raw.replace(':', '-') : ticker.value
  if (!tvSymbol || ['CASH', 'USD'].includes(tvSymbol)) return null
  return `https://cn.tradingview.com/symbols/${encodeURIComponent(tvSymbol)}/`
})
</script>

<template>
  <a
    v-if="href"
    class="symbol-cell symbol-cell--link"
    :href="href"
    target="_blank"
    rel="noopener noreferrer"
    @click.stop
  >
    <div class="symbol-logo">
      <img v-if="logoUrl" :src="logoUrl" :alt="symbol" />
      <span v-else>{{ ticker.slice(0, 2) }}</span>
    </div>
    <div class="symbol-meta">
      <div class="code">{{ ticker }}</div>
      <div class="name">{{ name || symbol }}</div>
    </div>
  </a>
  <div v-else class="symbol-cell">
    <div class="symbol-logo">
      <img v-if="logoUrl" :src="logoUrl" :alt="symbol" />
      <span v-else>{{ ticker.slice(0, 2) }}</span>
    </div>
    <div class="symbol-meta">
      <div class="code">{{ ticker }}</div>
      <div class="name">{{ name || symbol }}</div>
    </div>
  </div>
</template>
