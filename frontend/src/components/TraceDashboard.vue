<script setup>
import { computed, onMounted, ref } from 'vue'
import TraceEventList from './TraceEventList.vue'

const emit = defineEmits(['resume'])

const traces = ref([])
const selected = ref(null)
const loading = ref(false)
const error = ref('')
const filter = ref({ workflow_type: '', status: '' })
const form = ref(defaultForm())

function defaultForm() {
  return {
    outcome: 'approved', requirement_fit: 'pass', code_quality: 'pass',
    review_effectiveness: '', diagnosis_effectiveness: '', adoption: 'all',
    fix_effectiveness: '', issue_types_text: '', note: '', preference_title: '',
    preference_content: '', preference_scope_type: 'project', preference_scope_value: '',
  }
}

function resumeSelected() {
  if (selected.value?.status === 'paused' && selected.value.session_id) {
    emit('resume', {
      session_id: selected.value.session_id,
      workflow_type: selected.value.workflow_type,
    })
  }
}

async function request(url, options = {}) {
  const response = await fetch(url, options)
  const data = await response.json()
  if (!response.ok || data.error) throw new Error(data.error || '请求失败')
  return data
}

async function loadTraces() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ limit: '100' })
    if (filter.value.workflow_type) params.set('workflow_type', filter.value.workflow_type)
    if (filter.value.status) params.set('status', filter.value.status)
    traces.value = await request(`/api/traces?${params}`)
  } catch (e) { error.value = e.message } finally { loading.value = false }
}

async function selectTrace(traceId) {
  try {
    selected.value = await request(`/api/traces/${encodeURIComponent(traceId)}`)
    form.value = defaultForm()
  } catch (e) { error.value = e.message }
}

async function saveLabel() {
  if (!selected.value) return
  const payload = { ...form.value }
  payload.issue_types = payload.issue_types_text.split(',').map(v => v.trim()).filter(Boolean)
  delete payload.issue_types_text
  try {
    await request(`/api/traces/${encodeURIComponent(selected.value.trace_id)}/labels`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    })
    await selectTrace(selected.value.trace_id)
    await loadTraces()
  } catch (e) { error.value = e.message }
}

const durationText = computed(() => {
  if (selected.value?.duration_ms == null) return '-'
  if (selected.value.duration_ms < 1000) return `${selected.value.duration_ms}ms`
  return `${(selected.value.duration_ms / 1000).toFixed(1)}s`
})
onMounted(loadTraces)
</script>

<template>
  <section class="trace-page">
    <div class="toolbar">
      <h2>任务 Trace</h2>
      <select v-model="filter.workflow_type" @change="loadTraces"><option value="">全部流程</option><option value="dev">DEV</option><option value="debug">DEBUG</option></select>
      <select v-model="filter.status" @change="loadTraces"><option value="">全部状态</option><option value="running">运行中</option><option value="paused">等待人工</option><option value="done">完成</option><option value="failed">失败</option></select>
      <button @click="loadTraces" :disabled="loading">刷新</button>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <div class="layout">
      <div class="trace-list">
        <button v-for="trace in traces" :key="trace.trace_id" class="trace-row" :class="{ selected: selected?.trace_id === trace.trace_id }" @click="selectTrace(trace.trace_id)">
          <strong>{{ trace.workflow_type.toUpperCase() }} · {{ trace.status }}</strong>
          <span>{{ trace.description }}</span><small>{{ trace.started_at }} · {{ trace.label_outcome || '未标注' }}</small>
        </button>
        <p v-if="!loading && !traces.length" class="empty">暂无工作流记录。</p>
      </div>
      <div v-if="selected" class="detail">
        <h3>{{ selected.trace_id }}</h3>
        <p>{{ selected.project_path || '未设置项目路径' }}</p>
        <div class="summary-grid">
          <article><small>状态</small><strong :class="`status-${selected.status}`">{{ selected.status }}</strong></article>
          <article><small>耗时</small><strong>{{ durationText }}</strong></article>
          <article><small>输入Token</small><strong>{{ selected.input_tokens ?? 0 }}</strong></article>
          <article><small>输出Token</small><strong>{{ selected.output_tokens ?? 0 }}</strong></article>
          <article><small>Trace事件</small><strong>{{ selected.events?.length ?? 0 }}</strong></article>
        </div>
        <div v-if="selected.status === 'paused'" class="resume-banner">
          <span>该任务正在等待人工操作</span>
          <button class="primary" type="button" @click="resumeSelected">打开人工介入</button>
        </div>
        <TraceEventList :events="selected.events || []" title="执行时间线" id-prefix="history" />
        <details v-if="selected.evaluations?.length"><summary>单任务质量评测（{{ selected.evaluations.length }}）</summary><div v-for="item in selected.evaluations" :key="item.metric_name" class="event"><b>{{ item.metric_name }}：{{ item.score ?? '-' }}</b><span>{{ item.source }}</span><pre>{{ JSON.stringify(item.evidence, null, 2) }}</pre></div></details>
        <details><summary>正式产出物（{{ selected.artifacts.length }}）</summary><div v-for="artifact in selected.artifacts" :key="artifact.created_at + artifact.type" class="artifact"><b>{{ artifact.type }}</b><pre>{{ artifact.content }}</pre></div></details>
        <form class="label-form" @submit.prevent="saveLabel">
          <h3>人工标注与验收反馈</h3>
          <div class="grid">
            <label>结果<select v-model="form.outcome"><option value="approved">通过</option><option value="conditional">有条件通过</option><option value="rejected">驳回</option></select></label>
            <label>代码采纳<select v-model="form.adoption"><option value="all">全部采纳</option><option value="partial">部分采纳</option><option value="none">未采纳</option></select></label>
            <label>需求符合度<select v-model="form.requirement_fit"><option value="pass">通过</option><option value="fail">不通过</option><option value="unknown">不确定</option></select></label>
            <label>修复有效性<select v-model="form.fix_effectiveness"><option value="">未适用</option><option value="effective">有效</option><option value="partial">部分有效</option><option value="ineffective">无效</option></select></label>
          </div>
          <label>问题类型（逗号分隔）<input v-model="form.issue_types_text" placeholder="需求误解, 测试不足" /></label>
          <label>验收说明<textarea v-model="form.note" rows="3" /></label>
          <h4>沉淀明确的人类偏好（可选）</h4>
          <label>偏好标题<input v-model="form.preference_title" placeholder="Vue 组件开发约定" /></label>
          <label>偏好内容<textarea v-model="form.preference_content" rows="3" placeholder="例如：前端组件使用 Composition API 与 script setup，不引入 Pinia。" /></label>
          <div class="grid"><label>作用范围<select v-model="form.preference_scope_type"><option value="global">全局</option><option value="project">项目</option><option value="directory">目录</option></select></label><label>范围路径<input v-model="form.preference_scope_value" :placeholder="selected.project_path" /></label></div>
          <button class="primary">保存反馈</button>
        </form>
      </div>
      <div v-else class="empty">选择一条任务查看完整执行记录。</div>
    </div>
  </section>
</template>

<style scoped>
.toolbar,.grid { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }.toolbar { margin-bottom:16px; }.toolbar h2 { margin-right:auto; }.toolbar select,.toolbar button,input,textarea { background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:8px; }.toolbar button,.primary { cursor:pointer; }.layout{display:grid;grid-template-columns:minmax(220px, .7fr) minmax(0,1.5fr);gap:16px}.trace-list,.detail{min-width:0}.trace-row{display:flex;width:100%;flex-direction:column;gap:4px;text-align:left;padding:10px;background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;margin-bottom:7px;cursor:pointer}.trace-row.selected{border-color:#58a6ff}.trace-row span{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.trace-row small,.detail>p,.event span{color:#8b949e;font-size:12px}.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:12px 0}.summary-grid article{background:#111418;border:1px solid #30363d;border-radius:7px;padding:9px}.summary-grid small{display:block;color:#8b949e;font-size:11px;margin-bottom:4px}.summary-grid strong{font-size:14px}.status-running{color:#58a6ff}.status-done{color:#3fb950}.status-failed{color:#f85149}.status-paused{color:#d29922}.detail details{border-top:1px solid #30363d;padding:10px 0}.event,.artifact{border-left:2px solid #30363d;padding:8px;margin:8px 0}.event pre,.artifact pre{white-space:pre-wrap;max-height:220px;overflow:auto;font-size:12px;color:#8b949e;margin-top:6px}.label-form{border-top:1px solid #30363d;margin-top:16px;padding-top:16px;display:flex;flex-direction:column;gap:10px}.label-form label{display:flex;flex-direction:column;gap:5px;font-size:13px;color:#8b949e}.grid>label{flex:1;min-width:140px}.primary{align-self:flex-start;background:#238636;border:0;border-radius:6px;color:white;padding:9px 16px}.error{color:#f85149}.empty{color:#8b949e;padding:24px;text-align:center;background:#161b22;border-radius:6px}@media(max-width:720px){.layout{grid-template-columns:1fr}}
.resume-banner { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:10px 0; color:#d29922; }
</style>
