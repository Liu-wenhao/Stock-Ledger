<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import SummaryCards from '@/components/SummaryCards.vue'
import { usePortfolioStore } from '@/stores/portfolio'
import { refreshMarket } from '@/api/portfolio'
import { isUsMarketOpen } from '@/utils/marketHours'

const store = usePortfolioStore()
const route = useRoute()
const router = useRouter()
const refreshing = ref(false)
const refreshVersion = ref(0)

const activeKey = computed({
  get: () => route.name,
  set: (name) => router.push({ name }),
})

async function onRefresh(notify = true) {
  refreshing.value = true
  try {
    const res = await refreshMarket(notify)
    await store.loadSummary()
    refreshVersion.value += 1
    if (notify) message.success(`行情及盈亏已更新（${res.priced}/${res.symbols}）`)
  } catch (e) {
    await store.loadSummary()
    refreshVersion.value += 1
  } finally {
    refreshing.value = false
  }
}

onMounted(() => {
  if (isUsMarketOpen()) onRefresh(false)
  else store.loadSummary()
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1>我的投资组合</h1>
      <a-button :loading="refreshing" @click="onRefresh()">刷新行情及盈亏</a-button>
    </div>

    <SummaryCards :summary="store.summary" :loading="store.loading" />

    <div class="panel main-panel">
      <a-tabs v-model:activeKey="activeKey">
        <a-tab-pane key="overview" tab="概览" />
        <a-tab-pane key="holdings" tab="持股" />
        <a-tab-pane key="trades" tab="交易" />
        <a-tab-pane key="analysis" tab="分析" />
      </a-tabs>
      <router-view :key="refreshVersion" @changed="store.loadSummary()" />
    </div>
  </div>
</template>

<style scoped>
.main-panel {
  width: 100%;
  max-width: none;
  max-height: none;
  padding-top: 4px;
}
</style>
