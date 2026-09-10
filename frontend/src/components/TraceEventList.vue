<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  events: { type: Array, default: () => [] },
  title: { type: String, default: '执行时间线' },
  idPrefix: { type: String, default: 'trace' },
})

const query = ref('')
const typeFilter = ref('')
const expandedIds = ref(new Set())
const showAll = ref(false)
const pageSize = 80

const eventTypes = computed(() => {
  const types = new Set(props.events.map(event => event.event_type).filter(Boolean))
  return [...types].sort()
})

const filteredEvents = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return props.events.filter(event => {
    if (typeFilter.value && event.event_type !== typeFilter.value) return false
    if (!keyword) return true
    const text = [event.event_type, event.stage, event.payload ? JSON.stringify(event.payload) : '']
      .filter(Boolean).join(' ').toLowerCase()
    return text.includes(keyword)
  })
})

const visibleEvents = computed(() => (
  showAll.value ? filteredEvents.value : filteredEvents.value.slice(0, pageSize)
))

const expandedCount = computed(() => (
  visibleEvents.value.filter(event => expandedIds.value.has(event.id)).length
))

function toggleEvent(eventId) {
  const next = new Set(expandedIds.value)
  if (next.has(eventId)) next.delete(eventId)
  else next.add(eventId)
  expandedIds.value = next
}

function expandAll() {
  expandedIds.value = new Set(visibleEvents.value.map(event => event.id))
}

function collapseAll() {
  expandedIds.value = new Set()
}

function eventPayload(event) {
  return JSON.stringify(event.payload || {}, null, 2)
}

function formatDuration(duration) {
  if (duration == null) return '-'
  if (duration < 1000) return `${duration}ms`
  return `${(duration / 1000).toFixed(duration % 1000 === 0 ? 0 : 1)}s`
}

function eventClass(event) {
  if (event.event_type?.includes('failed') || event.event_type === 'workflow_failed') return 'failed'
  if (event.event_type?.startsWith('rag_')) return 'rag'
  if (event.event_type?.startsWith('llm_')) return 'llm'
  if (event.event_type?.startsWith('review')) return 'review'
  if (event.event_type?.startsWith('stage_')) return 'stage'
  return 'default'
}
</script>

<template>
  <section class="trace-events">
    <details>
      <summary>
        <span>{{ title }}（{{ filteredEvents.length }}）</span>
        <small v-if="expandedCount">已展开 {{ expandedCount }}</small>
      </summary>

      <div class="controls">
        <input v-model="query" type="search" placeholder="搜索事件、阶段或内容" />
        <select v-model="typeFilter">
          <option value="">全部类型</option>
          <option v-for="type in eventTypes" :key="type" :value="type">{{ type }}</option>
        </select>
        <button type="button" @click="expandAll">展开全部</button>
        <button type="button" @click="collapseAll">收起全部</button>
      </div>

      <p v-if="!filteredEvents.length" class="empty">没有匹配的 Trace 事件。</p>
      <div v-else class="event-list">
        <article v-for="event in visibleEvents" :key="`${idPrefix}-${event.id}`" class="event-row">
          <button type="button" class="event-head" @click="toggleEvent(event.id)">
            <span class="event-type" :class="eventClass(event)">{{ event.event_type }}</span>
            <span class="event-stage">{{ event.stage || '-' }}</span>
            <span class="event-duration">{{ formatDuration(event.duration_ms) }}</span>
            <span class="event-time">{{ event.created_at || '' }}</span>
            <span class="toggle">{{ expandedIds.has(event.id) ? '收起' : '展开' }}</span>
          </button>
          <pre v-if="expandedIds.has(event.id)">{{ eventPayload(event) }}</pre>
        </article>
      </div>

      <button
        v-if="filteredEvents.length > pageSize"
        type="button"
        class="more"
        @click="showAll = !showAll"
      >
        {{ showAll ? '收起长列表' : `显示全部 ${filteredEvents.length} 条` }}
      </button>
    </details>
  </section>
</template>

<style scoped>
.trace-events { border-top: 1px solid #30363d; padding: 10px 0; }
details summary {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  color: #c9d1d9;
  font-size: 14px;
  padding: 6px 0;
}
details summary::marker { color: #58a6ff; }
details summary small { color: #8b949e; font-size: 12px; }
.controls { display: grid; grid-template-columns: minmax(180px, 1fr) 170px auto auto; gap: 8px; margin: 10px 0; }
.controls input, .controls select, .more {
  background: #161b22;
  color: #c9d1d9;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 7px 9px;
  font-size: 12px;
}
.controls button, .more { cursor: pointer; white-space: nowrap; }
.controls button:hover, .more:hover { border-color: #58a6ff; color: #fff; }
.event-list { display: flex; flex-direction: column; gap: 6px; }
.event-row { border: 1px solid #21262d; border-radius: 6px; overflow: hidden; background: #111418; }
.event-head {
  display: grid;
  grid-template-columns: 150px minmax(90px, 1fr) 70px 170px 46px;
  gap: 8px;
  align-items: center;
  width: 100%;
  padding: 8px 9px;
  background: transparent;
  border: 0;
  color: #c9d1d9;
  text-align: left;
  cursor: pointer;
  font-size: 12px;
}
.event-head:hover { background: #161b22; }
.event-type { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.event-type.stage { color: #58a6ff; }
.event-type.llm { color: #a371f7; }
.event-type.rag { color: #2f81f7; }
.event-type.review { color: #d29922; }
.event-type.failed { color: #f85149; }
.event-type.default { color: #c9d1d9; }
.event-stage, .event-duration, .event-time { color: #8b949e; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.toggle { color: #58a6ff; text-align: right; }
pre {
  margin: 0;
  padding: 10px;
  border-top: 1px solid #21262d;
  color: #8b949e;
  background: #0d1117;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow: auto;
  max-height: 320px;
}
.empty { color: #8b949e; padding: 16px; text-align: center; }
.more { margin: 10px auto; display: block; }
@media (max-width: 720px) {
  .controls { grid-template-columns: 1fr 1fr; }
  .event-head { grid-template-columns: 1fr 1fr; }
  .event-duration, .event-time { display: none; }
  .toggle { text-align: left; }
}
</style>
