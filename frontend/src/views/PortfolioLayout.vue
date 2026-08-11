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

const activeKey = computed({
  get: () => route.name,
  set: (name) => router.push({ name }),
})

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
      <SummaryCards :summary="store.summary" :loading="store.loading" />
      <a-tabs v-model:activeKey="activeKey" class="portfolio-tabs">
        <a-tab-pane key="overview" tab="概览" />
        <a-tab-pane key="stats" tab="统计" />
        <a-tab-pane key="holdings" tab="持股" />
        <a-tab-pane key="trades" tab="交易" />
        <a-tab-pane key="prices" tab="价格" />
        <a-tab-pane key="analysis" tab="分析" />
        <a-tab-pane key="vibe" tab="AI 研究" />
      </a-tabs>
    </div>

    <div class="main-panel">
      <router-view :key="refreshVersion" @changed="store.loadSummary()" />
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
