<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  CheckCircleFilled,
  CloseCircleFilled,
  FileTextOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import {
  cancelVibeSession,
  fetchVibeMessages,
  fetchVibeSession,
  fetchVibeStatus,
  startVibeAnalysis,
} from '@/api/portfolio'

const presets = [
  {
    label: '交易行为诊断',
    prompt: '分析我的交易行为，识别反复出现的盈利模式、亏损模式、追涨杀跌和持仓纪律问题。',
  },
  {
    label: '影子策略对比',
    prompt: '用相同标的和资金约束构造一个纪律化影子策略，与实际交易结果比较并解释差异。',
  },
  {
    label: '风险与回撤',
    prompt: '分析交易记录中的集中度、单笔风险、连续亏损和回撤来源，并给出可量化的风险边界。',
  },
  {
    label: '策略改进',
    prompt: '基于全部交易证据，提出三项最值得优先验证的策略改进，并说明回测方法与失效条件。',
  },
]

const prompt = ref(presets[0].prompt)
const includeTrades = ref(true)
const service = ref({ configured: false, available: false, url: '', detail: '' })
const checking = ref(false)
const starting = ref(false)
const cancelling = ref(false)
const polling = ref(false)
const session = ref(null)
const messages = ref([])
const runError = ref('')
let pollTimer = null

const normalizedStatus = computed(() => String(session.value?.status || '').toLowerCase())
const assistantMessages = computed(() => messages.value.filter((item) => item.role === 'assistant'))
const running = computed(() => {
  if (assistantMessages.value.length) return false
  if (normalizedStatus.value === 'idle') return polling.value
  return ['active', 'queued', 'pending', 'running', 'processing', 'working'].includes(normalizedStatus.value)
})
const busy = computed(() => starting.value || running.value)
const statusLabel = computed(() => {
  const labels = {
    active: '研究中',
    queued: '排队中',
    pending: '准备中',
    running: '研究中',
    processing: '研究中',
    working: '研究中',
    completed: '已完成',
    idle: polling.value ? '等待中' : '已完成',
    failed: '失败',
    cancelled: '已取消',
    canceled: '已取消',
  }
  if (starting.value) return '提交中'
  if (assistantMessages.value.length && !['failed', 'cancelled', 'canceled'].includes(normalizedStatus.value)) {
    return '已完成'
  }
  return labels[normalizedStatus.value] || (session.value ? '已提交' : '未开始')
})
const emptyResult = computed(() => {
  if (['cancelled', 'canceled'].includes(normalizedStatus.value)) {
    return { title: '研究已取消', detail: '可以调整研究问题后重新提交' }
  }
  if (normalizedStatus.value === 'failed') {
    return { title: '研究未完成', detail: '检查服务状态后重新提交' }
  }
  if (['completed', 'idle'].includes(normalizedStatus.value) && session.value) {
    return { title: '未返回研究内容', detail: 'Vibe-Trading 已结束任务，但没有返回分析文本' }
  }
  return { title: '暂无研究结果', detail: '选择研究任务后开始分析' }
})

function stopPolling() {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = null
}

function schedulePoll() {
  stopPolling()
  pollTimer = window.setTimeout(pollSession, 1800)
}

async function checkService() {
  checking.value = true
  try {
    service.value = await fetchVibeStatus()
  } catch (error) {
    service.value = {
      configured: false,
      available: false,
      url: '',
      detail: error?.response?.data?.detail || '无法检查 Vibe-Trading 服务',
    }
  } finally {
    checking.value = false
  }
}

async function pollSession() {
  if (!session.value?.session_id) return
  try {
    const [nextSession, nextMessages] = await Promise.all([
      fetchVibeSession(session.value.session_id),
      fetchVibeMessages(session.value.session_id),
    ])
    session.value = nextSession
    messages.value = nextMessages
    runError.value = ''
    const terminal = ['completed', 'failed', 'cancelled', 'canceled', 'idle'].includes(normalizedStatus.value)
    polling.value = !terminal && !assistantMessages.value.length
    if (polling.value) schedulePoll()
  } catch (error) {
    runError.value = error?.response?.data?.detail || '获取研究进度失败'
    polling.value = true
    schedulePoll()
  }
}

async function startAnalysis() {
  const question = prompt.value.trim()
  if (!question) {
    message.warning('请输入研究问题')
    return
  }
  if (!service.value.available) {
    message.error('Vibe-Trading 服务当前不可用')
    return
  }

  stopPolling()
  polling.value = false
  starting.value = true
  runError.value = ''
  messages.value = []
  session.value = null
  try {
    const created = await startVibeAnalysis({
      prompt: question,
      include_trades: includeTrades.value,
    })
    session.value = created
    polling.value = true
    schedulePoll()
  } catch (error) {
    polling.value = false
    runError.value = error?.response?.data?.detail || '提交研究任务失败'
  } finally {
    starting.value = false
  }
}

async function cancelAnalysis() {
  if (!session.value?.session_id) return
  cancelling.value = true
  try {
    await cancelVibeSession(session.value.session_id)
    stopPolling()
    polling.value = false
    runError.value = ''
    session.value = { ...session.value, status: 'cancelled' }
    message.success('研究任务已取消')
  } catch (error) {
    runError.value = error?.response?.data?.detail || '取消研究任务失败，任务仍在运行'
    polling.value = true
    schedulePoll()
  } finally {
    cancelling.value = false
  }
}

onMounted(checkService)
onUnmounted(stopPolling)
</script>

<template>
  <div class="vibe-page">
    <section class="service-strip" aria-live="polite">
      <div class="service-state">
        <CheckCircleFilled v-if="service.available" class="state-icon state-icon--online" />
        <CloseCircleFilled v-else class="state-icon state-icon--offline" />
        <div>
          <strong>{{ service.available ? 'Vibe-Trading 已连接' : 'Vibe-Trading 未连接' }}</strong>
          <div
            class="service-detail"
            :title="service.detail || service.url || '未配置服务地址'"
          >
            {{ service.detail || service.url || '未配置服务地址' }}
          </div>
        </div>
      </div>
      <a-tooltip title="重新检查连接">
        <a-button
          type="text"
          class="icon-button"
          :loading="checking"
          aria-label="重新检查连接"
          @click="checkService"
        >
          <template #icon><ReloadOutlined /></template>
        </a-button>
      </a-tooltip>
    </section>

    <div class="research-workspace">
      <section class="research-controls" aria-labelledby="research-title">
        <div class="section-heading">
          <div>
            <h2 id="research-title">研究任务</h2>
            <p>Vibe-Trading 只读分析</p>
          </div>
          <a-switch
            v-model:checked="includeTrades"
            class="data-switch"
            checked-children="交易数据"
            un-checked-children="无数据"
            :disabled="busy"
          />
        </div>

        <div class="preset-list" aria-label="快捷研究问题">
          <button
            v-for="item in presets"
            :key="item.label"
            type="button"
            class="preset-button"
            :class="{ active: prompt === item.prompt }"
            :aria-pressed="prompt === item.prompt"
            :disabled="busy"
            @click="prompt = item.prompt"
          >
            {{ item.label }}
          </button>
        </div>

        <a-textarea
          v-model:value="prompt"
          class="research-input"
          :maxlength="5000"
          :auto-size="{ minRows: 7, maxRows: 14 }"
          placeholder="研究问题"
          aria-label="研究问题"
          :disabled="busy"
        />

        <div class="action-row">
          <span class="data-note">
            <FileTextOutlined />
            {{ includeTrades ? '附带全部交易 CSV' : '不附带交易数据' }}
          </span>
          <a-button
            v-if="running"
            danger
            :loading="cancelling"
            @click="cancelAnalysis"
          >
            <template #icon><StopOutlined /></template>
            取消研究
          </a-button>
          <a-button
            v-else
            type="primary"
            :loading="starting"
            :disabled="!service.available || !prompt.trim()"
            @click="startAnalysis"
          >
            <template #icon><PlayCircleOutlined /></template>
            开始研究
          </a-button>
        </div>
      </section>

      <section class="research-output" aria-labelledby="output-title" aria-live="polite">
        <div class="output-heading">
          <div>
            <h2 id="output-title">研究结果</h2>
            <p v-if="session?.title">{{ session.title }}</p>
          </div>
          <span
            class="run-status"
            :class="`run-status--${starting ? 'running' : normalizedStatus || 'empty'}`"
          >
            <span class="status-dot" />
            {{ statusLabel }}
          </span>
        </div>

        <a-alert v-if="runError" type="error" show-icon :message="runError" class="run-error" />

        <div v-if="busy && !assistantMessages.length" class="output-waiting">
          <a-spin size="small" />
          <span>{{ starting ? '正在提交研究任务' : '正在读取交易记录并生成研究结果' }}</span>
        </div>

        <div v-else-if="assistantMessages.length" class="message-list">
          <article v-for="(item, index) in assistantMessages" :key="item.message_id || index" class="result-message">
            <div class="message-meta">Vibe-Trading</div>
            <div class="message-content">{{ item.content }}</div>
          </article>
        </div>

        <div v-else class="output-empty">
          <FileTextOutlined />
          <strong>{{ emptyResult.title }}</strong>
          <span>{{ emptyResult.detail }}</span>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.vibe-page {
  --vibe-muted: #626772;
  padding: 4px 0 28px;
  color: var(--text);
}

.service-strip {
  display: flex;
  min-height: 58px;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: #fafbfc;
}

.service-state {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
}

.service-state > div {
  min-width: 0;
}

.state-icon {
  flex: 0 0 auto;
  font-size: 18px;
}

.state-icon--online {
  color: var(--green);
}

.state-icon--offline {
  color: var(--red);
}

.service-state strong {
  display: block;
  font-size: 14px;
  line-height: 1.4;
}

.service-detail {
  margin-top: 2px;
  overflow: hidden;
  color: var(--vibe-muted);
  font-size: 12px;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.icon-button {
  width: 44px;
  min-width: 44px;
  height: 44px;
}

.research-workspace {
  display: grid;
  grid-template-columns: minmax(320px, 0.82fr) minmax(420px, 1.18fr);
  min-height: 520px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 6px;
}

.research-controls,
.research-output {
  min-width: 0;
  padding: 20px;
}

.research-controls {
  border-right: 1px solid var(--border);
}

.research-output {
  background: #fafbfc;
}

.section-heading,
.output-heading,
.action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.section-heading h2,
.output-heading h2 {
  margin: 0;
  color: var(--text);
  font-size: 16px;
  font-weight: 700;
  line-height: 1.4;
}

.section-heading p,
.output-heading p {
  margin: 2px 0 0;
  color: var(--vibe-muted);
  font-size: 12px;
  line-height: 1.4;
}

.preset-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 18px 0 12px;
}

.preset-button {
  min-height: 44px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: #434651;
  background: #fff;
  font: inherit;
  font-size: 13px;
  line-height: 1.35;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease, color 0.15s ease;
}

.preset-button:hover,
.preset-button.active {
  border-color: #9aabff;
  color: #1945d8;
  background: #f3f6ff;
}

.preset-button:disabled,
.preset-button:disabled:hover {
  border-color: var(--border);
  color: #8a8f99;
  background: #f5f6f8;
  cursor: not-allowed;
}

.research-input {
  resize: vertical;
}

.research-input :deep(textarea) {
  line-height: 1.65;
}

.research-input :deep(textarea::placeholder) {
  color: var(--vibe-muted);
  opacity: 1;
}

.action-row {
  margin-top: 14px;
}

.data-note {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
  color: var(--vibe-muted);
  font-size: 12px;
}

.action-row :deep(.ant-btn) {
  min-width: 112px;
  min-height: 44px;
}

.output-heading {
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}

.run-status {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 7px;
  color: var(--vibe-muted);
  font-size: 12px;
  font-weight: 600;
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #b2b5be;
}

.run-status--running .status-dot,
.run-status--active .status-dot,
.run-status--processing .status-dot,
.run-status--working .status-dot,
.run-status--pending .status-dot,
.run-status--queued .status-dot {
  background: var(--blue);
  box-shadow: 0 2px 7px rgba(41, 98, 255, 0.35);
  animation: status-pulse 1.6s ease-out infinite;
}

.run-status--completed .status-dot,
.run-status--idle .status-dot {
  background: var(--green);
}

.run-status--failed .status-dot,
.run-status--cancelled .status-dot,
.run-status--canceled .status-dot {
  background: var(--red);
}

.run-error {
  margin-top: 16px;
}

.output-waiting,
.output-empty {
  display: flex;
  min-height: 390px;
  align-items: center;
  justify-content: center;
  color: var(--vibe-muted);
}

.output-waiting {
  gap: 10px;
  font-size: 13px;
}

.output-empty {
  flex-direction: column;
  gap: 8px;
  text-align: center;
}

.output-empty > :first-child {
  margin-bottom: 4px;
  color: #9da1ab;
  font-size: 28px;
}

.output-empty strong {
  color: #434651;
  font-size: 14px;
}

.output-empty span {
  font-size: 12px;
}

.message-list {
  padding-top: 18px;
}

.result-message + .result-message {
  margin-top: 24px;
  padding-top: 24px;
  border-top: 1px solid var(--border);
}

.message-meta {
  margin-bottom: 8px;
  color: var(--vibe-muted);
  font-size: 12px;
  font-weight: 600;
}

.message-content {
  max-width: 78ch;
  color: #242733;
  font-size: 14px;
  line-height: 1.75;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

@keyframes status-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(41, 98, 255, 0.32);
  }
  70%,
  100% {
    box-shadow: 0 0 0 7px rgba(41, 98, 255, 0);
  }
}

@media (max-width: 900px) {
  .data-switch {
    position: relative;
  }

  .data-switch::before {
    position: absolute;
    inset: -11px -4px;
    content: '';
  }

  .research-workspace {
    grid-template-columns: 1fr;
  }

  .research-controls {
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }

  .output-waiting,
  .output-empty {
    min-height: 260px;
  }
}

@media (max-width: 560px) {
  .vibe-page {
    padding-top: 0;
  }

  .service-strip {
    margin-bottom: 12px;
    padding: 8px 10px;
  }

  .service-detail {
    max-width: 70vw;
    overflow: visible;
    text-overflow: clip;
    white-space: normal;
    overflow-wrap: anywhere;
  }

  .research-workspace {
    margin-inline: calc(-1 * max(12px, env(safe-area-inset-left)));
    border-right: 0;
    border-left: 0;
    border-radius: 0;
  }

  .research-controls,
  .research-output {
    padding: 16px max(12px, env(safe-area-inset-right)) 18px max(12px, env(safe-area-inset-left));
  }

  .preset-list {
    gap: 6px;
    margin-top: 14px;
  }

  .preset-button {
    min-height: 44px;
    padding-inline: 8px;
    font-size: 12px;
  }

  .action-row {
    align-items: stretch;
    flex-direction: column;
  }

  .action-row :deep(.ant-btn) {
    width: 100%;
    min-height: 44px;
  }

  .output-heading {
    align-items: flex-start;
  }

  .output-waiting,
  .output-empty {
    min-height: 220px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .status-dot {
    animation: none !important;
  }
}
</style>
