<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import SummaryCards from '@/components/SummaryCards.vue'
import { usePortfolioStore } from '@/stores/portfolio'
import { refreshMarket } from '@/api/portfolio'
import { isUsMarketOpen } from '@/utils/marketHours'

const store = usePortfolioStore()
const route = useRoute()
const router = useRouter()
const refreshVersion = ref(0)

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
  refreshVersion.value += 1
  await store.loadSummary()
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
  if (isUsMarketOpen()) refreshQuotes()
  else store.loadSummary()
})
</script>

<template>
  <div class="page">
    <div class="sticky-top">
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
  background: rgba(255, 255, 255, 0.98);
  backdrop-filter: blur(10px);
}

.market-switch {
  display: flex;
  gap: 6px;
  width: fit-content;
  margin: 2px 0 12px;
  padding: 4px;
  border: 1px solid #e7ebf2;
  border-radius: 12px;
  background: #f4f7fb;
}

.market-switch button {
  min-width: 72px;
  height: 32px;
  padding: 0 16px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: #5c677a;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}

.market-switch button.active {
  background: #fff;
  color: #14977d;
  box-shadow: 0 1px 2px rgba(36, 48, 71, 0.08);
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
