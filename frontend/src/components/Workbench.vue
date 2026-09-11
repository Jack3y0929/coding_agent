<script setup>
import { ref } from 'vue'

const emit = defineEmits(['started'])

const workflowType = ref('auto')
const description = ref('')
const projectPath = ref('../')
const loading = ref(false)
const error = ref('')

function selectType(type) {
  workflowType.value = type
  error.value = ''
}

async function submitWorkflow() {
  if (!description.value.trim()) {
    error.value = '请输入需求描述'
    return
  }

  loading.value = true
  error.value = ''

  try {
    const res = await fetch('/api/workflow/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workflow_type: workflowType.value === 'auto' ? null : workflowType.value,
        description: description.value,
        project_path: projectPath.value,
      }),
    })
    const text = await res.text()
    let data = {}
    if (text.trim()) {
      try {
        data = JSON.parse(text)
      } catch {
        throw new Error(`服务器返回了非 JSON 响应（HTTP ${res.status}）`)
      }
    }
    if (!res.ok) {
      throw new Error(data.detail || data.error || `请求失败（HTTP ${res.status}）`)
    }
    if (data.error) {
      error.value = data.error
    } else {
      emit('started', {
        session_id: data.session_id,
        workflow_type: workflowType.value,
        project_path: projectPath.value,
      })
    }
  } catch (e) {
    error.value = '请求失败: ' + e.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="workbench">
    <div class="type-selector">
      <button
        class="type-btn"
        :class="{ active: workflowType === 'auto' }"
        @click="selectType('auto')"
      >
        <span class="icon">&#10024;</span>
        <span class="label">自动识别</span>
        <span class="desc">由意图槽位决定流程</span>
      </button>
      <button
        class="type-btn"
        :class="{ active: workflowType === 'dev' }"
        @click="selectType('dev')"
      >
        <span class="icon">&#9881;</span>
        <span class="label">我要开发</span>
        <span class="desc">新增功能 / 模块开发</span>
      </button>
      <button
        class="type-btn"
        :class="{ active: workflowType === 'debug' }"
        @click="selectType('debug')"
      >
        <span class="icon">&#9888;</span>
        <span class="label">我要修BUG</span>
        <span class="desc">问题诊断 / 代码修复</span>
      </button>
    </div>

    <div class="input-section">
      <label class="input-label">
        {{ workflowType === 'debug' ? 'BUG描述' : '任务描述' }}
      </label>
      <textarea
        v-model="description"
        class="input-textarea"
        :placeholder="workflowType === 'debug'
          ? '例如：给审批模块加一个批量通过功能，支持多选一键审批'
          : '例如：审批流并发操作时会重复审批，复现步骤是...'"
        rows="4"
      ></textarea>

      <label class="input-label">项目路径</label>
      <input
        v-model="projectPath"
        class="input-field"
        placeholder="项目根目录路径"
      />

      <div class="submit-row">
        <button
          class="submit-btn"
          :disabled="loading"
          @click="submitWorkflow"
        >
          {{ loading ? '提交中...' : '开始执行' }}
        </button>
      </div>

      <p v-if="error" class="error-message">{{ error }}</p>
    </div>
  </div>
</template>

<style scoped>
.workbench { max-width: 600px; margin: 0 auto; }

.type-selector {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-bottom: 24px;
}

.type-btn {
  background: #161b22;
  border: 2px solid #30363d;
  border-radius: 12px;
  padding: 24px 16px;
  cursor: pointer;
  text-align: center;
  transition: all 0.2s;
  color: #c9d1d9;
}
.type-btn:hover { border-color: #58a6ff; background: #1c2333; }
.type-btn.active { border-color: #58a6ff; background: #13233a; }
.type-btn .icon { display: block; font-size: 28px; margin-bottom: 8px; }
.type-btn .label { display: block; font-size: 18px; font-weight: 600; margin-bottom: 4px; }
.type-btn .desc { display: block; font-size: 12px; color: #8b949e; }
@media (max-width: 600px) { .type-selector { grid-template-columns: 1fr; } }

.input-section { display: flex; flex-direction: column; gap: 8px; }

.input-label { font-size: 14px; color: #8b949e; font-weight: 500; }

.input-textarea, .input-field {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 12px;
  color: #c9d1d9;
  font-size: 14px;
  font-family: inherit;
  resize: vertical;
  width: 100%;
}
.input-textarea:focus, .input-field:focus {
  outline: none;
  border-color: #58a6ff;
}
.input-field { padding: 10px 12px; }

.submit-row { margin-top: 12px; text-align: right; }

.submit-btn {
  background: #238636;
  border: none;
  border-radius: 8px;
  padding: 10px 28px;
  color: #fff;
  font-size: 15px;
  cursor: pointer;
  font-weight: 600;
  transition: background 0.2s;
}
.submit-btn:hover { background: #2ea043; }
.submit-btn:disabled { background: #30363d; cursor: not-allowed; }

.error-message { color: #f85149; font-size: 13px; margin-top: 8px; }
</style>
