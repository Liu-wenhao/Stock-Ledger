<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
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
const sideGroup = ref('trades')
const keyword = ref('')
const sideType = ref('')
const loading = ref(false)
const rows = ref([])
const modalOpen = ref(false)
const editRecord = ref(null)
const tableScrollY = useTableScrollY(420)

const pagination = reactive({
  current: 1,
  pageSize: 20,
  showSizeChanger: true,
  showTotal: (total) => `共 ${total} 条`,
  pageSizeOptions: ['10', '20', '50', '100'],
})

const tableScroll = computed(() => ({ x: 1100, y: tableScrollY.value }))

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

const columns = [
  { title: '商品代码', key: 'symbol', dataIndex: 'symbol', width: 220 },
  { title: '类型', key: 'side', dataIndex: 'side', width: 100 },
  { title: '日期', key: 'closing_time', dataIndex: 'closing_time', width: 160 },
  { title: '数量', key: 'qty', dataIndex: 'qty', align: 'right' },
  { title: '价格', key: 'fill_price', dataIndex: 'fill_price', align: 'right' },
  { title: '手续费', key: 'commission', dataIndex: 'commission', align: 'right' },
  { title: '总计', key: 'total', dataIndex: 'total', align: 'right' },
  { title: '笔记', key: 'notes', dataIndex: 'notes' },
  { title: '操作', key: 'actions', width: 90, align: 'center' },
]

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

watch(sideType, () => {
  resetPage()
  load()
})

onMounted(load)
</script>

<template>
  <div>
    <a-tabs v-model:activeKey="sideGroup" size="small" @change="onTabChange">
      <a-tab-pane key="trades" tab="交易" />
      <a-tab-pane key="dividends" tab="股息" />
    </a-tabs>

    <div class="toolbar">
      <a-space>
        <a-select
          v-model:value="sideType"
          style="width: 140px"
          :options="typeOptions"
          placeholder="类型"
        />
        <a-input-search
          v-model:value="keyword"
          placeholder="搜索商品代码"
          style="width: 240px"
          allow-clear
          @search="
            () => {
              resetPage()
              load()
            }
          "
        />
      </a-space>
      <a-space>
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

    <a-table
      :columns="columns"
      :data-source="rows"
      :loading="loading"
      row-key="id"
      :pagination="pagination"
      :scroll="tableScroll"
      size="middle"
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
          {{ dayjs(record.closing_time).format('YYYY年M月D日') }}
        </template>
        <template v-else-if="column.key === 'qty'">
          {{ ['Deposit', 'Withdrawal'].includes(record.side) ? '—' : formatQty(record.qty) }}
        </template>
        <template v-else-if="column.key === 'fill_price'">
          {{ formatMoney(record.fill_price).replace(' USD', '') }}
        </template>
        <template v-else-if="column.key === 'commission'">
          {{ formatMoney(record.commission) }}
        </template>
        <template v-else-if="column.key === 'total'">{{ formatMoney(record.total) }}</template>
        <template v-else-if="column.key === 'actions'">
          <a-dropdown>
            <a class="ant-dropdown-link" @click.prevent>···</a>
            <template #overlay>
              <a-menu>
                <a-menu-item @click="openEdit(record)">
                  <EditOutlined /> 编辑交易...
                </a-menu-item>
                <a-menu-item danger @click="onRemove(record)">
                  <DeleteOutlined /> 移除
                </a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
        </template>
      </template>
    </a-table>

    <TradeFormModal v-model:open="modalOpen" :edit-record="editRecord" @saved="onSaved" />
  </div>
</template>
