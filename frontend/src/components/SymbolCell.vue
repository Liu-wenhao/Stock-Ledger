<script setup>
import { tickerOf } from '@/utils/format'

defineProps({
  symbol: String,
  name: String,
  logoUrl: String,
})
</script>

<template>
  <div class="symbol-cell">
    <div class="symbol-logo">
      <img v-if="logoUrl" :src="logoUrl" :alt="symbol" />
      <span v-else>{{ tickerOf(symbol).slice(0, 2) }}</span>
    </div>
    <div class="symbol-meta">
      <div class="code">{{ tickerOf(symbol) }}</div>
      <div class="name">{{ name || symbol }}</div>
    </div>
  </div>
</template>
