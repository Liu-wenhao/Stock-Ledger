<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import SummaryCards from '@/components/SummaryCards.vue'
import { usePortfolioStore } from '@/stores/portfolio'
import { useTheme } from '@/composables/useTheme'
import { refreshMarket } from '@/api/portfolio'
import { isCnLunch, isMarketOpen } from '@/utils/marketHours'

const store = usePortfolioStore()
const { isDark } = useTheme()
const route = useRoute()
const router = useRouter()
const refreshVersion = ref(0)
const QUOTE_REFRESH_MS = 10_000
let quoteTimer
let quotePollBusy = false

async function pollQuotes() {
  if (quotePollBusy) return
  if (!isMarketOpen(store.market)) {
    if (store.market === 'cn' && isCnLunch()) store.bumpQuoteTick()
    return
  }
  quotePollBusy = true
  try {
    await refreshMarket(true)
    await store.loadSummary({ silent: true })
    store.bumpQuoteTick()
  } catch {
    // The next interval retries while the session is still open.
  } finally {
    quotePollBusy = false
  }
}

const markets = [
  { id: 'us', label: '美股' },
  { id: 'cn', label: 'A股' },
  { id: 'hk', label: '港股' },
]

const activeKey = computed({
  get: () => route.name,
  set: (name) => router.push({ name }),
})

async function switchMarket(id) {
  if (id === store.market) return
  store.setMarket(id)
  if (isMarketOpen(id)) await refreshQuotes()
  else {
    refreshVersion.value += 1
    await store.loadSummary()
  }
}

async function refreshQuotes() {
  try {
    await refreshMarket(false)
    await store.loadSummary()
    refreshVersion.value += 1
  } catch {
    await store.loadSummary()
    refreshVersion.value += 1
  }
}

onMounted(() => {
  if (isMarketOpen(store.market)) refreshQuotes()
  else store.loadSummary()
  quoteTimer = window.setInterval(pollQuotes, QUOTE_REFRESH_MS)
})
onUnmounted(() => {
  if (!quoteTimer) return
  window.clearInterval(quoteTimer)
  quoteTimer = undefined
})
</script>

<template>
  <div class="page">
    <div class="sticky-top">
      <div class="top-bar">
        <div class="market-switch" role="tablist" aria-label="市场">
          <button
            v-for="item in markets"
            :key="item.id"
            type="button"
            role="tab"
            :aria-selected="store.market === item.id"
            :class="{ active: store.market === item.id }"
            @click="switchMarket(item.id)"
          >
            {{ item.label }}
          </button>
        </div>
        <span
          class="theme-toggle"
          role="img"
          :aria-label="isDark ? '深色模式，20:00 至 08:00' : '浅色模式，08:00 至 20:00'"
          :title="isDark ? '20:00–08:00 深色' : '08:00–20:00 浅色'"
        >
          <svg v-if="isDark" viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.1 5.1l1.6 1.6M17.3 17.3l1.6 1.6M18.9 5.1l-1.6 1.6M6.7 17.3l-1.6 1.6" />
          </svg>
          <svg v-else viewBox="0 0 24 24" aria-hidden="true">
            <path d="M16.5 13.2A6.2 6.2 0 0 1 10.8 5 6.4 6.4 0 1 0 16.5 13.2z" />
          </svg>
        </span>
      </div>
      <SummaryCards :summary="store.summary" :loading="store.loading" />
      <a-tabs v-model:activeKey="activeKey" class="portfolio-tabs">
        <a-tab-pane key="overview" tab="概览" />
        <a-tab-pane key="stats" tab="统计" />
        <a-tab-pane key="holdings" tab="持股" />
        <a-tab-pane key="prices" tab="价格" />
        <a-tab-pane key="review" tab="复盘" />
        <a-tab-pane key="strategy" tab="策略" />
        <a-tab-pane key="trades" tab="交易" />
        <a-tab-pane key="analysis" tab="分析" />
      </a-tabs>
    </div>

    <div class="main-panel">
      <router-view :key="`${store.market}-${refreshVersion}`" @changed="store.loadSummary()" />
    </div>
  </div>
</template>

<style scoped>
.sticky-top {
  position: sticky;
  top: 0;
  z-index: 30;
  margin-inline: -20px;
  padding-inline: 20px;
  background: var(--sticky);
  backdrop-filter: blur(10px);
}

.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 2px 0 12px;
}

.market-switch {
  display: flex;
  gap: 6px;
  width: fit-content;
  margin: 0;
  padding: 4px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--chip);
}

.market-switch button {
  min-width: 72px;
  height: 32px;
  padding: 0 16px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--muted);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}

.market-switch button.active {
  background: var(--panel);
  color: #14977d;
  box-shadow: 0 1px 2px rgba(36, 48, 71, 0.08);
}

.theme-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex: none;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--chip);
  color: var(--text);
}

.theme-toggle svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.sticky-top :deep(.portfolio-tabs .ant-tabs-content-holder) {
  display: none;
}

.sticky-top :deep(.ant-tabs-nav) {
  margin-bottom: 12px !important;
}

.main-panel {
  width: 100%;
  max-width: none;
  max-height: none;
  padding-top: 16px;
}

@media (max-width: 800px) {
  .page-header h1 {
    font-size: 17px;
  }

  .sticky-top {
    position: static;
    z-index: auto;
    backdrop-filter: none;
    margin-inline: calc(-1 * max(16px, env(safe-area-inset-left)))
      calc(-1 * max(16px, env(safe-area-inset-right)));
    padding-inline: max(16px, env(safe-area-inset-left)) max(16px, env(safe-area-inset-right));
  }
}

@media (max-width: 560px) {
  .page-header {
    margin-bottom: 12px;
  }

  .sticky-top {
    margin-inline: calc(-1 * max(12px, env(safe-area-inset-left)))
      calc(-1 * max(12px, env(safe-area-inset-right)));
    padding-inline: max(12px, env(safe-area-inset-left)) max(12px, env(safe-area-inset-right));
  }
}
</style>
