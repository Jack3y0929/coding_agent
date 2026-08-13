<script setup>
import { onMounted, ref } from 'vue'

const memories = ref([])
const error = ref('')

async function load() {
  try {
    const response = await fetch('/api/memories')
    const data = await response.json()
    if (!response.ok || data.error) throw new Error(data.error || '加载失败')
    memories.value = data
  } catch (e) { error.value = e.message }
}

async function update(memory, status) {
  try {
    const response = await fetch(`/api/memories/${memory.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: memory.content, status }) })
    const data = await response.json()
    if (!response.ok || data.error) throw new Error(data.error || '更新失败')
    await load()
  } catch (e) { error.value = e.message }
}
onMounted(load)
</script>

<template>
  <section class="preferences"><div class="toolbar"><h2>开发偏好记忆</h2><button @click="load">刷新</button></div><p>仅展示由人工验收或运营标注明确沉淀的偏好。停用后不会再注入后续任务。</p><p v-if="error" class="error">{{ error }}</p><article v-for="memory in memories" :key="memory.id" :class="{ inactive: memory.status !== 'active' }"><header><strong>{{ memory.title }}</strong><span>{{ memory.status }} · {{ memory.scope_type }} {{ memory.scope_value || '全部' }}</span></header><textarea v-model="memory.content" rows="3"/><footer><small>来源 {{ memory.source_trace_id || '-' }} · 置信度 {{ memory.confidence }}</small><button @click="update(memory, memory.status === 'active' ? 'disabled' : 'active')">{{ memory.status === 'active' ? '停用' : '启用' }}</button><button @click="update(memory, memory.status)">保存文本</button></footer></article><p v-if="!memories.length" class="empty">暂无人工确认的开发偏好。</p></section>
</template>

<style scoped>
.toolbar{display:flex;align-items:center;margin-bottom:12px}.toolbar h2{margin-right:auto}.toolbar button,article button{background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:7px 10px;cursor:pointer}.preferences>p{color:#8b949e;font-size:13px;margin:8px 0}article{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px;margin:10px 0}article.inactive{opacity:.55}header,footer{display:flex;justify-content:space-between;gap:10px;align-items:center}header span,small{font-size:12px;color:#8b949e}textarea{margin:10px 0;width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:8px;resize:vertical;font:inherit}.error{color:#f85149!important}.empty{text-align:center;padding:25px;background:#161b22;border-radius:6px}
</style>
