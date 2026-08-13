<script setup>
import { ref } from 'vue'
import Workbench from './components/Workbench.vue'
import ProgressPanel from './components/ProgressPanel.vue'
import TraceDashboard from './components/TraceDashboard.vue'
import EvaluationPanel from './components/EvaluationPanel.vue'
import PreferencePanel from './components/PreferencePanel.vue'

const view = ref('workbench')
const sessionId = ref('')
const workflowType = ref('')

function onWorkflowStarted(data) {
  sessionId.value = data.session_id
  workflowType.value = data.workflow_type
  view.value = 'progress'
}

function onBackToWorkbench() {
  view.value = 'workbench'
  sessionId.value = ''
}

function navigate(target) {
  view.value = target
}
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
      <Workbench v-if="view === 'workbench'" @started="onWorkflowStarted" />
      <ProgressPanel v-else-if="view === 'progress'"
        :session-id="sessionId"
        :workflow-type="workflowType"
        @back="onBackToWorkbench"
      />
      <TraceDashboard v-else-if="view === 'traces'" />
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
.app-nav { display: flex; justify-content: center; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
.app-nav button { background: transparent; border: 1px solid #30363d; border-radius: 6px; color: #8b949e; cursor: pointer; padding: 7px 12px; }
.app-nav button:hover, .app-nav button.active { color: #c9d1d9; border-color: #58a6ff; background: #13233a; }
</style>
