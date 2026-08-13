<script setup>
import { ref } from 'vue'

const report = ref(null)
const loading = ref(false)
const error = ref('')
const workflowType = ref('')

async function run() {
  loading.value = true; error.value = ''
  try {
    const query = workflowType.value ? `?workflow_type=${workflowType.value}` : ''
    const response = await fetch(`/api/evaluations/run${query}`, { method: 'POST' })
    const data = await response.json()
    if (!response.ok || data.error) throw new Error(data.error || '评测失败')
    report.value = data
  } catch (e) { error.value = e.message } finally { loading.value = false }
}
</script>

<template>
  <section class="evaluation">
    <div class="toolbar"><h2>质量评测</h2><select v-model="workflowType"><option value="">全部流程</option><option value="dev">DEV</option><option value="debug">DEBUG</option></select><button @click="run" :disabled="loading">{{ loading ? '评测中...' : '生成报告' }}</button></div>
    <p>评测仅汇总已持久化的 Trace 与人工标注，避免将模型的内部推理作为评价数据。</p>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="report" class="content"><div class="metric-grid"><article v-for="(value, key) in report.metrics" :key="key"><small>{{ key }}</small><strong>{{ value ?? '-' }}</strong></article></div><h3>纳入评测的任务</h3><table><thead><tr><th>Trace</th><th>类型</th><th>状态</th><th>人工结果</th><th>耗时</th></tr></thead><tbody><tr v-for="item in report.traces" :key="item.trace_id"><td>{{ item.trace_id.slice(0, 16) }}</td><td>{{ item.workflow_type }}</td><td>{{ item.status }}</td><td>{{ item.label_outcome || '未标注' }}</td><td>{{ item.duration_ms ?? '-' }} ms</td></tr></tbody></table></div>
  </section>
</template>

<style scoped>
.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:12px}.toolbar h2{margin-right:auto}.toolbar select,.toolbar button{background:#161b22;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:8px}.toolbar button{cursor:pointer;background:#238636;border:0}.evaluation>p{color:#8b949e;font-size:13px;margin:8px 0}.error{color:#f85149!important}.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:8px;margin:20px 0}.metric-grid article{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px}.metric-grid small{display:block;color:#8b949e;word-break:break-all}.metric-grid strong{font-size:20px;display:block;margin-top:6px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px;border-bottom:1px solid #30363d;text-align:left;word-break:break-all}@media(max-width:600px){table{font-size:11px}th,td{padding:6px}}
</style>
