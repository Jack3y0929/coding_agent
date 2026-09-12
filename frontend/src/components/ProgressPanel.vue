<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import StageStepper from './StageStepper.vue'
import TraceEventList from './TraceEventList.vue'

const props = defineProps({
  sessionId: String,
  workflowType: String,
})

const emit = defineEmits(['back', 'finished'])

const stages = ref([])
const currentStage = ref('')
const currentMessage = ref('')
const tokenStatus = ref(null)
const isComplete = ref(false)
const isError = ref(false)
const isPaused = ref(false)
const pauseStage = ref('')
const summaryMessage = ref('')
const errorMessage = ref('')
const traceEvents = ref([])
const acceptance = ref(defaultAcceptance())
const intentInfo = ref(null)
const intentValidation = ref(null)
const slotForm = ref(defaultSlots())
const selectedClarifications = ref({})
const logInput = ref('')

function defaultAcceptance() {
  return {
    outcome: 'approved', requirement_fit: 'pass', code_quality: 'pass',
    review_effectiveness: '', diagnosis_effectiveness: '', adoption: 'all',
    fix_effectiveness: '', issue_types_text: '', note: '', preference_title: '',
    preference_content: '', preference_scope_type: 'project', preference_scope_value: '',
  }
}

function defaultSlots() {
  return {
    intent_workflow_type: '', target_modules_text: '', change_scope_text: '',
    protected_paths_text: '', acceptance_criteria_text: '', tech_constraints_text: '',
    validation_commands_text: '', reproduction_steps_text: '', observed_behavior: '',
    expected_behavior: '',
  }
}

function splitLines(value) {
  return value.split(/\n|,/).map(item => item.trim()).filter(Boolean)
}

let ws = null
let reconnectTimer = null
let statusTimer = null

const statusColor = computed(() => {
  if (isError.value) return '#f85149'
  if (isComplete.value) return '#3fb950'
  if (isPaused.value) return '#d29922'
  return '#58a6ff'
})

const statusText = computed(() => {
  if (isError.value) return '异常'
  if (isComplete.value) return '完成'
  if (isPaused.value) return '等待'
  return '运行中'
})

const costPercent = computed(() => {
  if (!tokenStatus.value) return 0
  return tokenStatus.value.usage_percent || 0
})

const currentWorkflowType = ref(props.workflowType || 'dev')

function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = `${protocol}//${location.host}/ws/progress/${props.sessionId}`

  ws = new WebSocket(url)

  ws.onopen = () => {
    console.log('WebSocket 已连接')
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      handleProgress(data)
    } catch (e) {
      console.error('消息解析失败:', e)
    }
  }

  ws.onclose = () => {
    console.log('WebSocket 已断开')
    if (!isComplete.value && !isError.value) {
      reconnectTimer = setTimeout(connectWebSocket, 2000)
    }
  }

  ws.onerror = (err) => {
    console.error('WebSocket 错误:', err)
  }
}

function handleProgress(data) {
  if (data.token_status) tokenStatus.value = data.token_status

  if (data.event === 'trace' && data.trace) {
    mergeTraceEvents([data.trace])
  }

  if (data.event === 'progress' && data.stage) {
    currentStage.value = data.stage
    currentMessage.value = data.stage_name || data.message || ''
    isPaused.value = false

    const existing = stages.value.find(s => s.key === data.stage)
    if (!existing) {
      stages.value.push({
        key: data.stage,
        name: data.stage_name || data.stage,
        done: false,
        active: true,
      })
    }
    stages.value.forEach(s => { s.active = s.key === data.stage })
  }

  if (data.stage === 'intent_clarify') {
    intentInfo.value = data.intent || intentInfo.value
    intentValidation.value = data.validation || intentValidation.value
    selectedClarifications.value = {}
    if (intentInfo.value) {
      slotForm.value = {
        intent_workflow_type: '',
        target_modules_text: (intentInfo.value.target_modules || []).join('\n'),
        change_scope_text: (intentInfo.value.change_scope || []).join('\n'),
        protected_paths_text: (intentInfo.value.protected_paths || []).join('\n'),
        acceptance_criteria_text: (intentInfo.value.acceptance_criteria || []).join('\n'),
        tech_constraints_text: (intentInfo.value.tech_constraints || []).join('\n'),
        validation_commands_text: (intentInfo.value.validation_commands || []).join('\n'),
        reproduction_steps_text: (intentInfo.value.reproduction_steps || []).join('\n'),
        observed_behavior: intentInfo.value.observed_behavior || '',
        expected_behavior: intentInfo.value.expected_behavior || '',
      }
    }
  }

  if (data.event === 'paused') {
    isPaused.value = true
    pauseStage.value = data.stage
    currentMessage.value = data.message
  }

  if (data.event === 'complete') {
    isComplete.value = true
    isPaused.value = false
    currentMessage.value = '工作流执行完成'
    if (data.summary) summaryMessage.value = data.summary
    stages.value.forEach(s => { s.done = true; s.active = false })
    emit('finished')
  }

  if (data.event === 'error') {
    isError.value = true
    errorMessage.value = data.message
    isPaused.value = false
    emit('finished')
  }

  if (data.event === 'trace' && data.trace?.stage) {
    normalizeStageFromTrace(data.trace)
  }
}

function normalizeStageFromTrace(trace) {
  if (!trace.stage) return
  if (trace.stage?.startsWith('review_')) {
    currentStage.value = 'review'
    markStageSeen('review')
    return
  }
  const knownStages = ['input_gate', 'analyze', 'develop_plan', 'develop_build', 'validate', 'review', 'fix', 'human_accept', 'output', 'diagnose', 'add_logging', 'human_wait']
  if (knownStages.includes(trace.stage)) {
    currentStage.value = trace.stage
    markStageSeen(trace.stage)
  }
}

function markStageSeen(stageKey) {
  if (!stages.value.some(stage => stage.key === stageKey)) {
    stages.value.push({ key: stageKey, name: stageKey, done: false, active: true })
  }
}

function mergeTraceEvents(events) {
  const merged = new Map(traceEvents.value.map(item => [item.id, item]))
  events.forEach(item => {
    if (item?.id != null) merged.set(item.id, item)
  })
  traceEvents.value = [...merged.values()].sort((a, b) => (a.id || 0) - (b.id || 0))
}

async function restoreTraceEvents() {
  if (!props.sessionId) return
  try {
    const response = await fetch(`/api/workflow/events/${encodeURIComponent(props.sessionId)}`)
    const data = await response.json()
    if (!data.error) mergeTraceEvents(data.events || [])
    const progressEvents = [...(data.events || [])]
      .filter(event => event.event_type === 'progress')
      .sort((a, b) => (a.id || 0) - (b.id || 0))
    const latest = progressEvents.at(-1)
    if (latest?.stage) {
      currentStage.value = latest.stage
      markStageSeen(latest.stage)
    }
    if (latest?.payload?.message) currentMessage.value = latest.payload.message
    const failedEvent = (data.events || []).find(event => event.event_type === 'workflow_failed')
    if (failedEvent) {
      isError.value = true
      errorMessage.value = failedEvent.payload?.error || '工作流执行失败'
    }
  } catch (error) {
    console.warn('恢复 Trace 事件失败:', error)
  }
}

function chooseClarification(option) {
  const value = option.value
  selectedClarifications.value = {
    ...selectedClarifications.value,
    [option.slot]: value,
  }
  if (option.slot === 'workflow_type') slotForm.value.intent_workflow_type = value
}

async function restoreStatus() {
  if (!props.sessionId) return
  try {
    const response = await fetch(`/api/workflow/status/${encodeURIComponent(props.sessionId)}`)
    const data = await response.json()
    if (data.error) return
    const progress = data.progress || {}
    if (progress.stage || progress.event) handleProgress(progress)
    if (data.status === 'done') handleProgress({ event: 'complete', stage: 'output' })
    if (data.status === 'failed') handleProgress({ event: 'error', message: '工作流执行失败' })
    if (data.type) currentWorkflowType.value = data.type
  } catch (error) {
    console.warn('恢复工作流状态失败:', error)
  }
}

function startStatusPolling() {
  if (statusTimer) clearTimeout(statusTimer)
  const poll = async () => {
    await restoreStatus()
    if (!isComplete.value && !isError.value) statusTimer = setTimeout(poll, 1500)
  }
  poll()
}

watch(() => props.workflowType, value => {
  if (value) currentWorkflowType.value = value
})

async function sendDecision(decision) {
  try {
    const payload = { session_id: props.sessionId, human_decision: decision }
    if (pauseStage.value === 'human_accept') {
      Object.assign(payload, acceptance.value, {
        issue_types: acceptance.value.issue_types_text.split(',').map(v => v.trim()).filter(Boolean),
      })
      if (decision === 'rejected') payload.outcome = 'rejected'
      delete payload.issue_types_text
    }
    if (pauseStage.value === 'human_wait') {
      payload.new_logs = logInput.value
    }
    if (pauseStage.value === 'intent_clarify') {
      Object.assign(payload, selectedClarifications.value)
      if (slotForm.value.intent_workflow_type) payload.intent_workflow_type = slotForm.value.intent_workflow_type
    }
    const response = await fetch('/api/workflow/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const result = await response.json()
    if (result.status !== 'resumed') {
      currentMessage.value = result.message || '恢复失败，请刷新后重试'
      return
    }
    isPaused.value = false
    logInput.value = ''
    currentMessage.value = ['approved', 'retry', 'slots_provided', 'logs_provided'].includes(decision)
      ? '已确认，继续执行...'
      : '已退回修复...'
  } catch (e) {
    console.error('发送决策失败:', e)
  }
}

function goBack() {
  if (ws) ws.close()
  emit('back')
}

onMounted(() => {
  connectWebSocket()
  startStatusPolling()
  restoreTraceEvents()
})

onUnmounted(() => {
  if (ws) ws.close()
  if (reconnectTimer) clearTimeout(reconnectTimer)
  if (statusTimer) clearTimeout(statusTimer)
})

watch(() => props.sessionId, () => {
  if (ws) ws.close()
  stages.value = []
  isComplete.value = false
  isError.value = false
  isPaused.value = false
  acceptance.value = defaultAcceptance()
  selectedClarifications.value = {}
  traceEvents.value = []
  logInput.value = ''
  connectWebSocket()
  startStatusPolling()
  restoreTraceEvents()
})
</script>

<template>
  <div class="progress-container">
    <div class="status-bar" :style="{ borderColor: statusColor }">
      <span class="status-dot" :style="{ background: statusColor }"></span>
      <span class="status-label">{{ statusText }}</span>
      <span class="status-message">{{ currentMessage }}</span>
    </div>

    <div class="token-bar">
      <div class="token-label">
        预算消耗
        <span class="token-value" v-if="tokenStatus">
          {{ tokenStatus.estimated_cost_yuan || 0 }} / {{ tokenStatus.max_cost_yuan || 5 }} 元
        </span>
      </div>
      <div class="bar-track">
        <div
          class="bar-fill"
          :style="{
            width: Math.min(costPercent, 100) + '%',
            background: costPercent > 80 ? '#f85149' : costPercent > 50 ? '#d29922' : '#3fb950'
          }"
        ></div>
      </div>
    </div>

    <StageStepper
      :workflow-type="currentWorkflowType"
      :stages="stages"
      :current-stage="currentStage"
      :current-message="currentMessage"
      :complete="isComplete"
      :failed="isError"
      :paused="isPaused"
    />

    <TraceEventList v-if="traceEvents.length" :events="traceEvents" title="实时 Trace" id-prefix="live" />

    <div v-if="isPaused" class="pause-section">
      <div class="pause-message">
        <template v-if="pauseStage === 'human_accept'">
          <p class="pause-title">等待你的验收确认</p>
          <p class="pause-hint">记录验收结论；明确的技术栈或写法偏好会成为后续任务可检索的长期记忆。</p>
          <div class="acceptance-form">
            <label>验收结论<select v-model="acceptance.outcome"><option value="approved">通过</option><option value="conditional">有条件通过</option><option value="rejected">驳回</option></select></label>
            <label>代码采纳<select v-model="acceptance.adoption"><option value="all">全部采纳</option><option value="partial">部分采纳</option><option value="none">未采纳</option></select></label>
            <label>问题类型（逗号分隔）<input v-model="acceptance.issue_types_text" placeholder="测试不足, 风格不符" /></label>
            <label>验收说明<textarea v-model="acceptance.note" rows="2" /></label>
            <label>偏好标题（可选）<input v-model="acceptance.preference_title" placeholder="后端开发约定" /></label>
            <label>明确偏好（可选）<textarea v-model="acceptance.preference_content" rows="2" placeholder="例如：接口参数使用 Pydantic 模型，IO 操作使用 async/await。" /></label>
            <label>作用范围<select v-model="acceptance.preference_scope_type"><option value="global">全局</option><option value="project">项目</option><option value="directory">目录</option></select></label>
            <label>范围路径（可选）<input v-model="acceptance.preference_scope_value" placeholder="默认当前任务项目" /></label>
          </div>
          <div class="pause-actions">
            <button class="btn-approve" @click="sendDecision('approved')">通过</button>
            <button class="btn-reject" @click="sendDecision('rejected')">不通过</button>
          </div>
        </template>
        <template v-else-if="pauseStage === 'intent_clarify'">
          <p class="pause-title">请选择缺失信息</p>
          <p class="pause-hint">系统已先从你的原始描述提取信息，下面只需要确认尚未明确的关键选项。</p>
          <div v-if="intentValidation" class="slot-warning">
            <strong>待确认：</strong>{{ intentValidation.missing_slots?.join('、') || '流程选择确认' }}
          </div>
          <div class="choice-groups">
            <div v-for="group in (intentValidation?.clarification_options || [])" :key="group.slot" class="choice-group">
              <strong>{{ group.label }}</strong>
              <div class="choice-list">
                <button
                  v-for="option in group.options"
                  :key="option.label"
                  type="button"
                  class="choice-btn"
                  :class="{ selected: JSON.stringify(selectedClarifications[group.slot]) === JSON.stringify(option.value) }"
                  @click="chooseClarification({ ...option, slot: group.slot })"
                >{{ option.label }}</button>
              </div>
            </div>
          </div>
          <div class="pause-actions"><button class="btn-approve" :disabled="!Object.keys(selectedClarifications).length" @click="sendDecision('slots_provided')">确认并继续</button></div>
        </template>
        <template v-else-if="pauseStage === 'human_intervene'">
          <p class="pause-title">自动修复已暂停</p>
          <p class="pause-hint">验证或审查未能在限定轮次内通过。你可以授权再尝试一次，或结束本次自动化任务并保留完整 Trace。</p>
          <div class="pause-actions">
            <button class="btn-approve" @click="sendDecision('retry')">授权重试</button>
            <button class="btn-reject" @click="sendDecision('stop')">结束任务</button>
          </div>
        </template>
        <template v-else>
          <p class="pause-title">等待复现BUG</p>
          <p class="pause-hint">日志已添加，请复现BUG后将新日志粘贴到下方</p>
          <textarea
            class="log-input"
            rows="4"
            placeholder="粘贴复现后的日志..."
            v-model="logInput"
          ></textarea>
          <div class="pause-actions">
            <button class="btn-approve" @click="sendDecision('logs_provided')">提交日志</button>
          </div>
        </template>
      </div>
    </div>

    <div v-if="isComplete" class="complete-section">
      <p class="complete-title">开发完成</p>
      <pre class="summary-text">{{ summaryMessage }}</pre>
      <button class="btn-back" @click="goBack">返回工作台</button>
    </div>

    <div v-if="isError" class="error-section">
      <p class="error-title">执行异常</p>
      <p class="error-text">{{ errorMessage }}</p>
      <button class="btn-back" @click="goBack">返回工作台</button>
    </div>
  </div>
</template>

<style scoped>
.progress-container { max-width: 600px; margin: 0 auto; }


.status-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px;
  background: #161b22;
  border: 1px solid;
  border-radius: 12px;
  margin-bottom: 16px;
}
.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}
.status-label { font-weight: 600; font-size: 14px; }
.status-message { font-size: 14px; color: #8b949e; flex: 1; }

.token-bar { margin-bottom: 16px; }
.token-label {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: #8b949e;
  margin-bottom: 6px;
}
.token-value { color: #c9d1d9; }
.bar-track {
  height: 6px;
  background: #21262d;
  border-radius: 3px;
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.5s;
}


.pause-section {
  background: #1a1f24;
  border: 1px solid #d29922;
  border-radius: 12px;
  padding: 20px;
  text-align: center;
  margin-top: 12px;
}
.pause-title { font-size: 16px; font-weight: 600; margin-bottom: 4px; }
.pause-hint { font-size: 13px; color: #8b949e; margin-bottom: 16px; }
.pause-actions { display: flex; gap: 12px; justify-content: center; }
.btn-approve {
  background: #238636;
  border: none;
  border-radius: 8px;
  padding: 8px 24px;
  color: #fff;
  cursor: pointer;
  font-size: 14px;
}
.btn-approve:hover { background: #2ea043; }
.btn-reject {
  background: #da3633;
  border: none;
  border-radius: 8px;
  padding: 8px 24px;
  color: #fff;
  cursor: pointer;
  font-size: 14px;
}
.btn-reject:hover { background: #f85149; }

.log-input {
  width: 100%;
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 10px;
  color: #c9d1d9;
  font-family: monospace;
  font-size: 13px;
  margin-bottom: 12px;
  resize: vertical;
}
.acceptance-form { display:grid; grid-template-columns:1fr 1fr; gap:9px; text-align:left; margin:12px 0; }
.acceptance-form label { display:flex; flex-direction:column; gap:4px; color:#8b949e; font-size:12px; }
.acceptance-form input,.acceptance-form textarea,.acceptance-form select { background:#161b22; border:1px solid #30363d; border-radius:6px; color:#c9d1d9; padding:7px; font:inherit; }
.acceptance-form textarea { resize:vertical; }
@media (max-width: 560px) { .acceptance-form { grid-template-columns:1fr; } }
.slot-warning { text-align:left; background:#211b10; border:1px solid #d29922; border-radius:6px; padding:10px; color:#d29922; font-size:13px; margin:12px 0; }.slot-warning ul { margin:6px 0 0 18px; color:#c9d1d9; }
.choice-groups { display:flex; flex-direction:column; gap:14px; margin:14px 0; text-align:left; }
.choice-group { display:flex; flex-direction:column; gap:7px; color:#c9d1d9; font-size:13px; }
.choice-list { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:7px; }
.choice-btn { background:#161b22; border:1px solid #30363d; border-radius:6px; color:#c9d1d9; cursor:pointer; padding:9px 10px; text-align:left; }
.choice-btn:hover,.choice-btn.selected { border-color:#58a6ff; background:#13233a; }
.btn-approve:disabled { opacity:.5; cursor:not-allowed; }
.slot-form { display:grid; grid-template-columns:1fr 1fr; gap:9px; text-align:left; margin:12px 0; }.slot-form label { display:flex; flex-direction:column; gap:4px; color:#8b949e; font-size:12px; }.slot-form input,.slot-form textarea,.slot-form select { background:#161b22; border:1px solid #30363d; border-radius:6px; color:#c9d1d9; padding:7px; font:inherit; }.slot-form textarea { resize:vertical; } @media (max-width:560px) { .slot-form { grid-template-columns:1fr; } }

.complete-section, .error-section {
  text-align: center;
  padding: 20px;
  margin-top: 12px;
}
.complete-title { font-size: 18px; color: #3fb950; margin-bottom: 8px; }
.error-title { font-size: 18px; color: #f85149; margin-bottom: 8px; }
.error-text { font-size: 13px; color: #8b949e; margin-bottom: 12px; }
.summary-text {
  text-align: left;
  background: #161b22;
  border-radius: 8px;
  padding: 12px;
  font-size: 12px;
  overflow-x: auto;
  margin-bottom: 12px;
  white-space: pre-wrap;
  color: #c9d1d9;
}

.btn-back {
  background: #21262d;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 8px 20px;
  color: #c9d1d9;
  cursor: pointer;
  font-size: 14px;
}
.btn-back:hover { background: #30363d; }
</style>
