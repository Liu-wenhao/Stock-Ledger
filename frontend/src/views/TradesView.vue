<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import dayjs from 'dayjs'
import { Modal, message } from 'ant-design-vue'
import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  PlusOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import {
  deleteTrade,
  exportTradesUrl,
  fetchTrades,
  importTrades,
} from '@/api/portfolio'
import SymbolCell from '@/components/SymbolCell.vue'
import TradeFormModal from '@/components/TradeFormModal.vue'
import { formatMoney, formatQty } from '@/utils/format'
import { useTableScrollY } from '@/composables/useTableScroll'

const emit = defineEmits(['changed'])
const route = useRoute()
const router = useRouter()
const sideGroup = ref('trades')
const keyword = ref('')
const sideType = ref('')
const loading = ref(false)
const rows = ref([])
const modalOpen = ref(false)
const editRecord = ref(null)
const highlightId = ref(null)
const tableScrollY = useTableScrollY(420)
const viewportWidth = ref(window.innerWidth)

const pagination = reactive({
  current: 1,
  pageSize: 20,
  showSizeChanger: true,
  showTotal: (total) => `共 ${total} 条`,
  pageSizeOptions: ['10', '20', '50', '100'],
})

const tableScroll = computed(() => ({
  x: viewportWidth.value <= 560 ? 860 : 1100,
  y: viewportWidth.value <= 560 ? undefined : tableScrollY.value,
}))

const SIDE_LABELS = {
  Buy: '买入',
  Sell: '卖出',
  Dividend: '股息',
  Deposit: '入金',
  Withdrawal: '出金',
}

const SIDE_COLORS = {
  Buy: 'success',
  Sell: 'error',
  Deposit: 'processing',
  Withdrawal: 'warning',
  Dividend: 'cyan',
}

const typeOptions = computed(() => {
  if (sideGroup.value === 'dividends') {
    return [{ value: '', label: '全部类型' }, { value: 'Dividend', label: '股息' }]
  }
  return [
    { value: '', label: '全部类型' },
    { value: 'Buy', label: '买入' },
    { value: 'Sell', label: '卖出' },
    { value: 'Deposit', label: '入金' },
    { value: 'Withdrawal', label: '出金' },
  ]
})

const baseColumns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', minWidth: 160, fixed: 'left' },
  { title: '类型', key: 'side', dataIndex: 'side', width: 100 },
  { title: '日期', key: 'closing_time', dataIndex: 'closing_time', width: 180 },
  { title: '价格', key: 'fill_price', dataIndex: 'fill_price', align: 'right' },
  { title: '数量', key: 'qty', dataIndex: 'qty', align: 'right' },
  { title: '手续费', key: 'commission', dataIndex: 'commission', align: 'right' },
  { title: '总额', key: 'total', dataIndex: 'total', align: 'right' },
  { title: '笔记', key: 'notes', dataIndex: 'notes' },
  { title: '操作', key: 'actions', width: 130, align: 'center' },
]

const compactColumnWidths = {
  symbol: 136,
  side: 72,
  closing_time: 128,
  fill_price: 88,
  qty: 70,
  commission: 84,
  total: 88,
  notes: 108,
  actions: 96,
}

const columns = computed(() => {
  if (viewportWidth.value > 560) return baseColumns
  return baseColumns.map((column) => {
    const width = compactColumnWidths[column.key]
    return width
      ? {
          ...column,
          width,
          minWidth: undefined,
          fixed: column.key === 'symbol' ? undefined : column.fixed,
        }
      : column
  })
})

function resetPage() {
  pagination.current = 1
}

async function load() {
  loading.value = true
  try {
    rows.value = await fetchTrades({
      side_group: sideGroup.value,
      q: keyword.value || undefined,
      side: sideType.value || undefined,
    })
  } finally {
    loading.value = false
  }
}

function onTabChange() {
  sideType.value = ''
  resetPage()
  load()
}

function onTableChange(pag) {
  pagination.current = pag.current
  pagination.pageSize = pag.pageSize
}

function openCreate() {
  editRecord.value = null
  modalOpen.value = true
}

function openEdit(record) {
  editRecord.value = record
  modalOpen.value = true
}

function customRow(record) {
  return {
    class: record.id === highlightId.value ? 'trade-row-highlight' : undefined,
  }
}

async function focusTradeById(rawId) {
  const id = Number(rawId)
  if (!Number.isFinite(id) || id <= 0) return

  keyword.value = ''
  sideType.value = ''

  // Probe both tabs so we land on the correct list
  let record = null
  for (const group of ['trades', 'dividends']) {
    sideGroup.value = group
    await load()
    record = rows.value.find((r) => Number(r.id) === id) || null
    if (record) break
  }
  if (!record) {
    message.warning('未找到该交易')
    await router.replace({ name: 'trades', query: {} })
    return
  }

  const wantGroup = record.side === 'Dividend' ? 'dividends' : 'trades'
  if (sideGroup.value !== wantGroup) {
    sideGroup.value = wantGroup
    await load()
    record = rows.value.find((r) => Number(r.id) === id) || record
  }

  const idx = rows.value.findIndex((r) => Number(r.id) === id)
  if (idx >= 0) {
    pagination.current = Math.floor(idx / pagination.pageSize) + 1
  }
  highlightId.value = id
  openEdit(record)
  await router.replace({ name: 'trades', query: {} })
  await nextTick()
  document.querySelector('.trade-row-highlight')?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

function onRemove(record) {
  Modal.confirm({
    title: '移除交易？',
    content: '此操作不可撤销',
    okType: 'danger',
    async onOk() {
      await deleteTrade(record.id)
      message.success('已移除')
      await load()
      emit('changed')
    },
  })
}

async function onUpload(file) {
  const res = await importTrades(file)
  message.success(`已导入 ${res.imported} 条（替换 ${res.deleted} 条）`)
  resetPage()
  await load()
  emit('changed')
  return false
}

function onSaved() {
  load()
  emit('changed')
}

function onViewportResize() {
  viewportWidth.value = window.innerWidth
}

watch(sideType, () => {
  resetPage()
  load()
})

watch(
  () => route.query.editId,
  async (editId) => {
    if (editId) await focusTradeById(editId)
  },
)

onMounted(async () => {
  window.addEventListener('resize', onViewportResize)
  await load()
  if (route.query.editId) await focusTradeById(route.query.editId)
})

onUnmounted(() => {
  window.removeEventListener('resize', onViewportResize)
})
</script>

<template>
  <div class="trades-view">
    <a-tabs v-model:activeKey="sideGroup" size="small" @change="onTabChange">
      <a-tab-pane key="trades" tab="交易" />
      <a-tab-pane key="dividends" tab="股息" />
    </a-tabs>

    <div class="toolbar trades-toolbar">
      <a-space class="filter-actions">
        <a-select
          v-model:value="sideType"
          class="type-select"
          :options="typeOptions"
          placeholder="类型"
        />
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          class="trade-search"
          allow-clear
          @search="
            () => {
              resetPage()
              load()
            }
          "
        />
      </a-space>
      <a-space class="primary-actions">
        <a-button type="primary" @click="openCreate">
          <PlusOutlined /> 添加交易
        </a-button>
        <a-upload :before-upload="onUpload" :show-upload-list="false" accept=".csv">
          <a-button>
            <UploadOutlined /> 上传
          </a-button>
        </a-upload>
        <a-button :href="exportTradesUrl">
          <DownloadOutlined /> 下载
        </a-button>
      </a-space>
    </div>

    <div class="mobile-scroll-hint">左右滑动查看价格、数量和操作</div>
    <a-table
      class="mobile-data-table trades-table"
      :columns="columns"
      :data-source="rows"
      :loading="loading"
      row-key="id"
      :pagination="pagination"
      :scroll="tableScroll"
      size="middle"
      :custom-row="customRow"
      @change="onTableChange"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'symbol'">
          <SymbolCell
            v-if="record.symbol"
            :symbol="record.symbol"
            :name="record.name"
            :logo-url="record.logo_url"
          />
          <span v-else class="muted">现金</span>
        </template>
        <template v-else-if="column.key === 'side'">
          <a-tag :color="SIDE_COLORS[record.side] || 'default'">
            {{ SIDE_LABELS[record.side] || record.side }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'closing_time'">
          {{ dayjs(record.closing_time).format('YYYY年M月D日 HH:mm') }}
        </template>
        <template v-else-if="column.key === 'qty'">
          {{ ['Deposit', 'Withdrawal'].includes(record.side) ? '—' : formatQty(record.qty) }}
        </template>
        <template v-else-if="column.key === 'fill_price'">
          {{ formatMoney(record.fill_price, 'USD', 4).replace(' USD', '') }}
        </template>
        <template v-else-if="column.key === 'commission'">
          {{ formatMoney(record.commission) }}
        </template>
        <template v-else-if="column.key === 'total'">{{ formatMoney(record.total) }}</template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="0">
            <a-button class="action-button" type="link" size="small" aria-label="编辑交易" title="编辑" @click="openEdit(record)">
              <EditOutlined />
            </a-button>
            <a-button class="action-button" type="link" size="small" danger aria-label="删除交易" title="删除" @click="onRemove(record)">
              <DeleteOutlined />
            </a-button>
          </a-space>
        </template>
      </template>
    </a-table>

    <TradeFormModal v-model:open="modalOpen" :edit-record="editRecord" @saved="onSaved" />
  </div>
</template>

<style scoped>
.type-select {
  width: 140px;
}

.trade-search {
  width: 240px;
}

.action-button {
  min-width: 40px;
}

:deep(.trade-row-highlight > td) {
  background: #e8f0fe !important;
}

@media (max-width: 800px) {
  .trades-toolbar,
  .filter-actions,
  .primary-actions {
    width: 100%;
  }

  .filter-actions,
  .primary-actions {
    display: grid;
    gap: 8px !important;
  }

  .filter-actions {
    grid-template-columns: minmax(120px, 0.8fr) minmax(180px, 1.2fr);
  }

  .primary-actions {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .filter-actions :deep(.ant-space-item),
  .primary-actions :deep(.ant-space-item),
  .primary-actions :deep(.ant-upload),
  .primary-actions :deep(.ant-btn) {
    width: 100%;
  }

  .type-select,
  .trade-search {
    width: 100%;
  }

}

@media (max-width: 560px) {
  .filter-actions {
    grid-template-columns: 1fr;
  }

  .primary-actions {
    grid-template-columns: 1fr 1fr;
  }

  .primary-actions :deep(.ant-space-item:first-child) {
    grid-column: 1 / -1;
  }

  .trades-table :deep(.ant-table-cell-fix-left) {
    box-shadow: 8px 0 12px -12px rgba(19, 23, 34, 0.5);
  }
}
</style>
