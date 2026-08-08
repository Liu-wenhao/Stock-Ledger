<script setup>
import { computed, reactive, ref, watch } from 'vue'
import dayjs from 'dayjs'
import { message } from 'ant-design-vue'
import { searchSymbols, createTrade, updateTrade } from '@/api/portfolio'

const props = defineProps({
  open: Boolean,
  editRecord: { type: Object, default: null },
})
const emit = defineEmits(['update:open', 'saved'])

const saving = ref(false)
const symbolOptions = ref([])
const form = reactive({
  side: 'Buy',
  symbol: '',
  closing_time: dayjs(),
  fill_price: null,
  qty: 1,
  commission: 0,
  notes: '',
})

const cashSides = ['Deposit', 'Withdrawal', 'Dividend']
const isCash = computed(() => cashSides.includes(form.side))
const total = computed(() => {
  const qty = Number(form.qty || 0)
  const price = Number(form.fill_price || 0)
  const fee = Number(form.commission || 0)
  if (isCash.value) return price
  if (form.side === 'Buy') return qty * price + fee
  return qty * price - fee
})

watch(
  () => props.open,
  async (v) => {
    if (!v) return
    if (props.editRecord) {
      Object.assign(form, {
        side: props.editRecord.side,
        symbol: props.editRecord.symbol || '',
        closing_time: dayjs(props.editRecord.closing_time),
        fill_price: Number(props.editRecord.fill_price),
        qty: Number(props.editRecord.qty),
        commission: Number(props.editRecord.commission),
        notes: props.editRecord.notes || '',
      })
    } else {
      Object.assign(form, {
        side: 'Buy',
        symbol: '',
        closing_time: dayjs(),
        fill_price: null,
        qty: 1,
        commission: 0,
        notes: '',
      })
    }
    await onSearch('')
  },
)

async function onSearch(q) {
  const rows = await searchSymbols(q || '')
  symbolOptions.value = rows.map((r) => ({
    value: r.symbol,
    label: r.name ? `${r.symbol} · ${r.name}` : r.symbol,
  }))
}

async function save(addMore = false) {
  const payload = {
    side: form.side,
    symbol: isCash.value && form.side !== 'Dividend' ? form.symbol || null : form.symbol || null,
    qty: form.qty ?? 0,
    fill_price: form.fill_price ?? 0,
    commission: form.commission ?? 0,
    closing_time: dayjs(form.closing_time).format('YYYY-MM-DDTHH:mm:ss'),
    notes: form.notes || null,
  }
  if (['Buy', 'Sell'].includes(form.side) && !payload.symbol) {
    message.warning('请选择商品代码')
    return
  }
  saving.value = true
  try {
    if (props.editRecord) {
      await updateTrade(props.editRecord.id, payload)
      message.success('已更新')
    } else {
      await createTrade(payload)
      message.success('已保存')
    }
    emit('saved')
    if (addMore && !props.editRecord) {
      form.fill_price = null
      form.qty = 1
      form.commission = 0
      form.notes = ''
    } else {
      emit('update:open', false)
    }
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    :title="editRecord ? '编辑交易' : '添加交易'"
    :footer="null"
    width="520px"
    wrap-class-name="mobile-sheet-modal trade-form-modal"
    centered
    destroy-on-close
    @cancel="emit('update:open', false)"
  >
    <a-segmented
      class="trade-side-segmented"
      v-model:value="form.side"
      block
      style="margin-bottom: 16px"
      :options="[
        { label: '买入', value: 'Buy' },
        { label: '卖出', value: 'Sell' },
        { label: '入金', value: 'Deposit' },
        { label: '出金', value: 'Withdrawal' },
        { label: '股息', value: 'Dividend' },
      ]"
    />
    <a-form layout="vertical" >
      <a-form-item v-if="!isCash || form.side === 'Dividend'" label="商品代码">
        <a-auto-complete
          v-model:value="form.symbol"
          :options="symbolOptions"
          placeholder="选择商品，如 NASDAQ:AAPL"
          style="width: 100%"
          @search="onSearch"
        />
      </a-form-item>
      <a-form-item label="日期">
        <a-date-picker v-model:value="form.closing_time" show-time style="width: 100%" />
      </a-form-item>
      <a-form-item :label="isCash ? '金额' : '价格'">
        <a-input-number v-model:value="form.fill_price" :min="0" :step="0.01" style="width: 100%" />
      </a-form-item>
      <a-form-item v-if="!isCash" label="数量">
        <a-input-number v-model:value="form.qty" :min="0" :step="0.0001" style="width: 100%" />
      </a-form-item>
      <a-form-item v-if="!isCash" label="手续费">
        <a-input-number v-model:value="form.commission" :min="0" :step="0.01" style="width: 100%" />
      </a-form-item>
      <a-form-item label="笔记">
        <a-textarea v-model:value="form.notes" :maxlength="128" show-count :rows="1" />
      </a-form-item>
      <div class="trade-total">共计 {{ total.toFixed(2) }} USD</div>
      <div class="trade-form-actions">
        <a-button @click="emit('update:open', false)">取消</a-button>
        <a-button type="primary" :loading="saving" @click="save(false)">保存</a-button>
        <a-button v-if="!editRecord" type="primary" :loading="saving" @click="save(true)">保存并添加更多</a-button>
      </div>
    </a-form>
  </a-modal>
</template>

<style scoped>
.trade-total {
  margin-bottom: 6px;
  font-weight: 600;
}

.trade-form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

@media (max-width: 560px) {
  .trade-form-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }

  .trade-form-actions :deep(.ant-btn) {
    width: 100%;
  }

  .trade-form-actions :deep(.ant-btn:last-child:nth-child(3)) {
    grid-column: 1 / -1;
  }
}
</style>
