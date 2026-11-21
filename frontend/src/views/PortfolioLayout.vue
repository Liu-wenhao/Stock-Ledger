<script setup>
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import SummaryCards from '@/components/SummaryCards.vue'
import { usePortfolioStore } from '@/stores/portfolio'
import { useTheme } from '@/composables/useTheme'
import { refreshMarket } from '@/api/portfolio'
import { isCnLunch, isMarketOpen } from '@/utils/marketHours'

const store = usePortfolioStore()
const { isDark, toggleTheme } = useTheme()
const route = useRoute()
const menuRef = ref(null)
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
  { id: 'us', label: '美股', short: '美' },
  { id: 'cn', label: 'A股', short: 'A' },
  { id: 'hk', label: '港股', short: '港' },
]

const menuItems = [
  { id: 'overview', label: '概览' },
  { id: 'stats', label: '统计' },
  { id: 'holdings', label: '持股' },
  { id: 'review', label: '复盘' },
  { id: 'strategy', label: '策略' },
  { id: 'trades', label: '交易' },
  { id: 'analysis', label: '分析' },
]

function revealActiveMenu() {
  nextTick(() => menuRef.value?.querySelector('.active')?.scrollIntoView({ block: 'nearest', inline: 'nearest' }))
}

watch(() => route.name, revealActiveMenu)

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
  revealActiveMenu()
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
        <nav ref="menuRef" class="top-menu" aria-label="主菜单">
          <router-link
            v-for="item in menuItems"
            :key="item.id"
            :to="{ name: item.id }"
            class="menu-link"
            :class="{ active: route.name === item.id }"
            :aria-current="route.name === item.id ? 'page' : undefined"
          >{{ item.label }}</router-link>
        </nav>
        <div class="top-controls">
          <div class="market-switch" role="tablist" aria-label="市场">
          <button
            v-for="item in markets"
            :key="item.id"
            type="button"
            role="tab"
            :aria-label="item.label"
            :aria-selected="store.market === item.id"
            :class="{ active: store.market === item.id }"
            @click="switchMarket(item.id)"
          >
            <span class="market-label">{{ item.label }}</span>
            <span class="market-short" aria-hidden="true">{{ item.short }}</span>
          </button>
          </div>
        <button
          type="button"
          class="theme-toggle"
          :aria-pressed="isDark"
          :aria-label="isDark ? '切换浅色主题' : '切换深色主题'"
          :title="isDark ? '切换浅色主题' : '切换深色主题'"
          @click="toggleTheme"
        >
          <svg v-if="isDark" viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.1 5.1l1.6 1.6M17.3 17.3l1.6 1.6M18.9 5.1l-1.6 1.6M6.7 17.3l-1.6 1.6" />
          </svg>
          <svg v-else viewBox="0 0 24 24" aria-hidden="true">
            <path d="M16.5 13.2A6.2 6.2 0 0 1 10.8 5 6.4 6.4 0 1 0 16.5 13.2z" />
          </svg>
        </button>
        </div>
      </div>
    </div>

    <SummaryCards v-if="route.name === 'overview'" :summary="store.summary" :loading="store.loading" />

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
  min-height: 56px;
  padding-block: 8px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 12px;
}

.top-menu {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}

.top-menu::-webkit-scrollbar {
  display: none;
}

.menu-link {
  flex: none;
  padding: 9px 14px;
  border-radius: 8px;
  color: var(--muted);
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
}

.menu-link:hover,
.menu-link.active {
  background: var(--chip);
  color: var(--text);
}

.menu-link.active {
  color: var(--blue);
}

.top-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
}

.market-short {
  display: none;
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
  min-width: 54px;
  height: 32px;
  padding: 0 12px;
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
  cursor: pointer;
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
    margin-inline: calc(-1 * max(16px, env(safe-area-inset-left)))
      calc(-1 * max(16px, env(safe-area-inset-right)));
    padding-inline: max(16px, env(safe-area-inset-left)) max(16px, env(safe-area-inset-right));
  }
}

@media (max-width: 560px) {
  .top-bar {
    gap: 8px;
  }

  .top-controls {
    gap: 6px;
  }

  .menu-link {
    padding: 9px 10px;
    font-size: 13px;
  }

  .market-switch {
    gap: 2px;
    padding: 3px;
  }

  .market-switch button {
    min-width: 28px;
    padding: 0 7px;
    font-size: 13px;
  }

  .market-label {
    display: none;
  }

  .market-short {
    display: inline;
  }

  .theme-toggle {
    width: 32px;
    height: 32px;
  }

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
