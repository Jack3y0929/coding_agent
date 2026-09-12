<script setup>
import { onMounted, ref } from 'vue'
import Workbench from './components/Workbench.vue'
import ProgressPanel from './components/ProgressPanel.vue'
import TraceDashboard from './components/TraceDashboard.vue'
import EvaluationPanel from './components/EvaluationPanel.vue'
import PreferencePanel from './components/PreferencePanel.vue'

const view = ref('workbench')
const sessionId = ref('')
const workflowType = ref('')
const activeTask = ref(null)
const ACTIVE_TASK_KEY = 'just-codding.active-task'

function saveActiveTask(data) {
  activeTask.value = { session_id: data.session_id, workflow_type: data.workflow_type || 'auto', project_path: data.project_path || '' }
  localStorage.setItem(ACTIVE_TASK_KEY, JSON.stringify(activeTask.value))
}

function clearActiveTask() {
  activeTask.value = null
  localStorage.removeItem(ACTIVE_TASK_KEY)
}

async function restoreActiveTask() {
  const raw = localStorage.getItem(ACTIVE_TASK_KEY)
  if (!raw) return
  try {
    const saved = JSON.parse(raw)
    if (!saved?.session_id) return clearActiveTask()
    const response = await fetch(`/api/workflow/status/${encodeURIComponent(saved.session_id)}`)
    const data = await response.json()
    if (data.error || ['done', 'failed'].includes(data.status)) return clearActiveTask()
    activeTask.value = { ...saved, workflow_type: data.type || saved.workflow_type, status: data.status }
    localStorage.setItem(ACTIVE_TASK_KEY, JSON.stringify(activeTask.value))
  } catch (error) {
    console.warn('恢复活动任务失败:', error)
  }
}

function onWorkflowStarted(data) {
  sessionId.value = data.session_id
  workflowType.value = data.workflow_type
  saveActiveTask(data)
  view.value = 'progress'
}

function onBackToWorkbench() {
  view.value = 'workbench'
  restoreActiveTask()
}

function navigate(target) {
  view.value = target
  if (target === 'workbench') restoreActiveTask()
}

function resumeActiveTask() {
  if (!activeTask.value?.session_id) return
  sessionId.value = activeTask.value.session_id
  workflowType.value = activeTask.value.workflow_type
  view.value = 'progress'
}

function onWorkflowFinished() {
  clearActiveTask()
}

function resumeTraceTask(task) {
  sessionId.value = task.session_id
  workflowType.value = task.workflow_type
  saveActiveTask(task)
  view.value = 'progress'
}

onMounted(() => {
  restoreActiveTask()
})
</script>

<template>
  <div class="app-container">
    <header class="app-header">
      <h1>Just_codding</h1>
      <span class="subtitle">AI 自动化代码开发助手</span>
    </header>
    <nav class="app-nav" aria-label="主导航">
      <button :class="{ active: view === 'workbench' }" @click="navigate('workbench')">工作台</button>
      <button :class="{ active: view === 'traces' }" @click="navigate('traces')">任务 Trace</button>
      <button :class="{ active: view === 'evaluation' }" @click="navigate('evaluation')">质量评测</button>
      <button :class="{ active: view === 'preferences' }" @click="navigate('preferences')">开发偏好</button>
    </nav>
    <main class="app-main">
      <section v-if="view === 'workbench' && activeTask" class="active-task">
        <div><strong>有一个任务正在运行</strong><span>会话 {{ activeTask.session_id }} · {{ activeTask.status === 'paused' ? '等待你的选择' : '执行中' }}</span></div>
        <button @click="resumeActiveTask">继续查看</button>
      </section>
      <Workbench v-if="view === 'workbench'" @started="onWorkflowStarted" />
      <ProgressPanel v-else-if="view === 'progress'"
        :session-id="sessionId"
        :workflow-type="workflowType"
        @back="onBackToWorkbench"
        @finished="onWorkflowFinished"
      />
      <TraceDashboard v-else-if="view === 'traces'" @resume="resumeTraceTask" />
      <EvaluationPanel v-else-if="view === 'evaluation'" />
      <PreferencePanel v-else-if="view === 'preferences'" />
    </main>
  </div>
</template>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; }

.app-container { max-width: 900px; margin: 0 auto; padding: 24px 16px; }

.app-header {
  text-align: center;
  padding: 32px 0 24px;
}
.app-header h1 {
  font-size: 32px;
  color: #58a6ff;
  margin-bottom: 4px;
}
.app-header .subtitle {
  font-size: 14px;
  color: #8b949e;
}

.app-main { min-height: 400px; }
.active-task { display:flex; align-items:center; justify-content:space-between; gap:16px; margin:0 auto 20px; max-width:600px; padding:12px 14px; border:1px solid #d29922; border-radius:8px; background:#211b10; }
.active-task div { display:flex; flex-direction:column; gap:4px; }
.active-task span { color:#d29922; font-size:12px; }
.active-task button { background:#238636; border:0; border-radius:6px; color:#fff; cursor:pointer; padding:8px 12px; }
.app-nav { display: flex; justify-content: center; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
.app-nav button { background: transparent; border: 1px solid #30363d; border-radius: 6px; color: #8b949e; cursor: pointer; padding: 7px 12px; }
.app-nav button:hover, .app-nav button.active { color: #c9d1d9; border-color: #58a6ff; background: #13233a; }
</style>
