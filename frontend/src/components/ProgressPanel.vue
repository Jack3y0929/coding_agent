<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'

const props = defineProps({
  sessionId: String,
  workflowType: String,
})

const emit = defineEmits(['back'])

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
const acceptance = ref(defaultAcceptance())
const intentInfo = ref(null)
const intentValidation = ref(null)
const slotForm = ref(defaultSlots())

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

const stageList = [
  { key: 'init', name: '初始化' },
  { key: 'input_gate', name: '需求澄清' },
  { key: 'analyze', name: '需求分析' },
  { key: 'develop_plan', name: '开发规划' },
  { key: 'develop_build', name: '开发编码' },
  { key: 'review', name: '代码审查' },
  { key: 'fix', name: '修复问题' },
  { key: 'human_accept', name: '人工验收' },
  { key: 'output', name: '生成总结' },
  { key: 'diagnose', name: 'BUG诊断' },
  { key: 'add_logging', name: '添加日志' },
  { key: 'validate', name: '验证变更' },
  { key: 'human_wait', name: '等待复现' },
  { key: 'human_intervene', name: '人工介入' },
]

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
  }

  if (data.event === 'error') {
    isError.value = true
    errorMessage.value = data.message
    isPaused.value = false
  }
}

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
    if (pauseStage.value === 'intent_clarify') {
      Object.assign(payload, {
        ...slotForm.value,
        target_modules: splitLines(slotForm.value.target_modules_text),
        change_scope: splitLines(slotForm.value.change_scope_text),
        protected_paths: splitLines(slotForm.value.protected_paths_text),
        acceptance_criteria: splitLines(slotForm.value.acceptance_criteria_text),
        tech_constraints: splitLines(slotForm.value.tech_constraints_text),
        validation_commands: splitLines(slotForm.value.validation_commands_text),
        reproduction_steps: splitLines(slotForm.value.reproduction_steps_text),
      })
      for (const key of Object.keys(payload)) {
        if (key.endsWith('_text')) delete payload[key]
      }
    }
    await fetch('/api/workflow/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    isPaused.value = false
    currentMessage.value = decision === 'approved' ? '已确认，继续执行...' : '已退回修复...'
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
})

onUnmounted(() => {
  if (ws) ws.close()
  if (reconnectTimer) clearTimeout(reconnectTimer)
})

watch(() => props.sessionId, () => {
  if (ws) ws.close()
  stages.value = []
  isComplete.value = false
  isError.value = false
  isPaused.value = false
  acceptance.value = defaultAcceptance()
  connectWebSocket()
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

    <div class="stage-list">
      <div
        v-for="stage in stageList"
        :key="stage.key"
        class="stage-item"
        :class="{
          active: currentStage === stage.key,
          done: stages.find(s => s.key === stage.key)?.done,
        }"
      >
        <span class="stage-dot" :class="{ filled: stages.find(s => s.key === stage.key) }"></span>
        <span class="stage-name">{{ stage.name }}</span>
        <span class="stage-check" v-if="stages.find(s => s.key === stage.key)?.done">&#10003;</span>
      </div>
    </div>

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
          <p class="pause-title">请补充研发任务信息</p>
          <p class="pause-hint">系统会基于你确认的槽位决定继续开发、进入调试或保持暂停。</p>
          <div v-if="intentValidation" class="slot-warning">
            <strong>待补充：</strong>{{ intentValidation.missing_slots?.join('、') || '流程选择确认' }}
            <ul><li v-for="question in intentValidation.clarification_questions" :key="question">{{ question }}</li></ul>
          </div>
          <div class="slot-form">
            <label>确认流程<select v-model="slotForm.intent_workflow_type"><option value="">保持自动识别</option><option value="dev">DEV：需求开发</option><option value="debug">DEBUG：BUG 修复</option></select></label>
            <label>目标模块 / 路径<textarea v-model="slotForm.target_modules_text" rows="2" placeholder="每行一个，例如 backend/api" /></label>
            <label>允许修改范围<textarea v-model="slotForm.change_scope_text" rows="2" placeholder="每行一个相对路径，例如 backend" /></label>
            <label>禁止修改范围<textarea v-model="slotForm.protected_paths_text" rows="2" placeholder="每行一个相对路径，例如 migrations" /></label>
            <label>验收标准<textarea v-model="slotForm.acceptance_criteria_text" rows="2" placeholder="每行一个可验证标准" /></label>
            <label>技术约束<textarea v-model="slotForm.tech_constraints_text" rows="2" placeholder="技术栈、兼容性、代码风格要求" /></label>
            <label>验证命令<textarea v-model="slotForm.validation_commands_text" rows="2" placeholder="例如 pytest；仍会经过白名单校验" /></label>
            <label>复现步骤 / 日志<textarea v-model="slotForm.reproduction_steps_text" rows="2" placeholder="DEBUG 任务必填；每行一个步骤或日志摘要" /></label>
            <label>实际表现<textarea v-model="slotForm.observed_behavior" rows="2" placeholder="DEBUG 任务必填" /></label>
            <label>预期表现<textarea v-model="slotForm.expected_behavior" rows="2" placeholder="可选" /></label>
          </div>
          <div class="pause-actions"><button class="btn-approve" @click="sendDecision('slots_provided')">确认并继续</button></div>
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

.stage-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 20px;
}
.stage-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  color: #484f58;
}
.stage-item.active { color: #c9d1d9; background: #13233a; }
.stage-item.done { color: #3fb950; }
.stage-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 2px solid #30363d;
  flex-shrink: 0;
}
.stage-dot.filled { background: #58a6ff; border-color: #58a6ff; }
.stage-item.done .stage-dot { background: #3fb950; border-color: #3fb950; }
.stage-name { flex: 1; }
.stage-check { font-size: 12px; }

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
