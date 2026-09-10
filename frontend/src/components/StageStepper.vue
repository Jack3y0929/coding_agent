<script setup>
import { computed } from 'vue'

const props = defineProps({
  workflowType: { type: String, default: '' },
  stages: { type: Array, default: () => [] },
  currentStage: { type: String, default: '' },
  currentMessage: { type: String, default: '' },
  complete: { type: Boolean, default: false },
  failed: { type: Boolean, default: false },
  paused: { type: Boolean, default: false },
})

const workflowPlans = {
  dev: [
    { key: 'input_gate', name: '需求澄清' },
    { key: 'analyze', name: '需求分析' },
    { key: 'develop_plan', name: '开发规划' },
    { key: 'develop_build', name: '开发编码' },
    { key: 'validate', name: '验证变更' },
    { key: 'review', name: '代码审查' },
    { key: 'fix', name: '修复问题' },
    { key: 'human_accept', name: '人工验收' },
    { key: 'output', name: '生成总结' },
  ],
  debug: [
    { key: 'input_gate', name: '需求澄清' },
    { key: 'diagnose', name: 'BUG诊断' },
    { key: 'add_logging', name: '添加日志' },
    { key: 'human_wait', name: '等待复现' },
    { key: 'fix', name: '修复问题' },
    { key: 'validate', name: '验证变更' },
    { key: 'review', name: '代码审查' },
    { key: 'human_accept', name: '人工验收' },
    { key: 'output', name: '生成总结' },
  ],
}

const plan = computed(() => workflowPlans[props.workflowType] || workflowPlans.dev)
const currentIndex = computed(() => plan.value.findIndex(stage => stage.key === props.currentStage))
const reachedIndex = computed(() => {
  let index = -1
  plan.value.forEach((stage, stageIndex) => {
    if (props.stages.some(item => item.key === stage.key)) index = stageIndex
  })
  return index
})
const progressPercent = computed(() => {
  if (props.complete) return 100
  if (props.failed) return Math.max(0, Math.round((reachedIndex.value + 1) / plan.value.length * 100))
  const index = currentIndex.value >= 0 ? currentIndex.value : reachedIndex.value
  return Math.max(0, Math.round(((index + 1) / plan.value.length) * 100))
})
const progressLabel = computed(() => {
  if (props.complete) return `已完成 ${plan.value.length} / ${plan.value.length}`
  const index = currentIndex.value >= 0 ? currentIndex.value : reachedIndex.value
  const step = Math.min(Math.max(1, index + 2), plan.value.length)
  return `第 ${step} / ${plan.value.length} 步`
})

function stageState(stage) {
  const index = plan.value.findIndex(item => item.key === stage.key)
  if (props.complete) return 'done'
  if (props.failed && index === reachedIndex.value) return 'failed'
  if (props.currentStage === stage.key) return props.paused ? 'paused' : 'active'
  if (index < reachedIndex.value || index < currentIndex.value) return 'done'
  if (props.stages.some(item => item.key === stage.key && item.done)) return 'done'
  return 'pending'
}
</script>

<template>
  <section class="stage-stepper" :class="{ failed, complete }">
    <div class="stage-overview">
      <div>
        <strong>{{ workflowType === 'debug' ? 'DEBUG 流程' : 'DEV 流程' }}</strong>
        <span>{{ progressLabel }}</span>
      </div>
      <div class="progress-text">{{ progressPercent }}%</div>
    </div>
    <div class="progress-track"><div class="progress-fill" :style="{ width: progressPercent + '%' }"></div></div>
    <p v-if="currentMessage" class="current-message">{{ currentMessage }}</p>
    <ol class="steps">
      <li
        v-for="stage in plan"
        :key="stage.key"
        :class="stageState(stage)"
      >
        <span class="stage-dot">{{ stageState(stage) === 'done' ? '✓' : stageState(stage) === 'failed' ? '!' : '' }}</span>
        <span class="stage-label">{{ stage.name }}</span>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.stage-stepper {
  background: #111418;
  border: 1px solid #30363d;
  border-radius: 10px;
  padding: 14px;
  margin-bottom: 16px;
}
.stage-stepper.complete { border-color: #238636; }
.stage-stepper.failed { border-color: #f85149; }
.stage-overview { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.stage-overview div { display: flex; align-items: baseline; gap: 10px; }
.stage-overview strong { color: #c9d1d9; font-size: 14px; }
.stage-overview span { color: #8b949e; font-size: 13px; }
.progress-text { color: #58a6ff; font-size: 18px; font-weight: 700; }
.progress-track { height: 7px; border-radius: 4px; background: #21262d; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #238636, #58a6ff); transition: width .4s ease; }
.stage-stepper.failed .progress-fill { background: #f85149; }
.stage-stepper.complete .progress-fill { background: #3fb950; }
.current-message { color: #8b949e; font-size: 13px; margin-top: 9px; overflow: hidden; text-overflow: ellipsis; }
.steps {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(104px, 1fr));
  gap: 7px;
  list-style: none;
  margin: 13px 0 0;
  padding: 0;
}
.steps li {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 40px;
  padding: 8px;
  border: 1px solid #21262d;
  border-radius: 7px;
  color: #6e7681;
  background: #0d1117;
  font-size: 12px;
}
.steps li.done { color: #3fb950; border-color: rgba(63, 185, 80, .35); }
.steps li.active { color: #fff; border-color: #58a6ff; background: #13233a; box-shadow: 0 0 0 1px rgba(88, 166, 255, .15); }
.steps li.paused { color: #d29922; border-color: #d29922; background: #211b10; }
.steps li.failed { color: #f85149; border-color: rgba(248, 81, 73, .5); background: #2d1315; }
.stage-dot {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1px solid currentColor;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  flex-shrink: 0;
}
.stage-label { line-height: 1.25; overflow: hidden; text-overflow: ellipsis; }
@media (max-width: 720px) {
  .steps { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
