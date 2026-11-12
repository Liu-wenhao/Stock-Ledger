<script setup>
import { computed, ref, watch } from 'vue'
import { fetchCnDesk, fetchCnStockFlow } from '@/api/portfolio'

const props = defineProps({
  symbols: { type: Array, default: () => [] },
})

const desk = ref(null)
const stocks = ref([])

const symbolKey = computed(() => props.symbols.join(','))

async function loadDesk() {
  try {
    desk.value = await fetchCnDesk()
  } catch {
    /* 卡片已去掉，失败时沿用上一次 */
  }
}

async function loadStocks() {
  if (!props.symbols.length) {
    stocks.value = []
    return
  }
  try {
    const book = await fetchCnStockFlow(props.symbols)
    stocks.value = book.items || []
  } catch {
    /* 个股资金失败时保留已有列 */
  }
}

const emit = defineEmits(['ready', 'flow'])

watch(desk, (value) => emit('ready', value))
watch(stocks, (items) => emit('flow', items))

watch(symbolKey, loadStocks, { immediate: true })
loadDesk()
</script>

<template>
  <span hidden></span>
</template>
