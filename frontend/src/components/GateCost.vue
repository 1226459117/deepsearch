<script setup>
import { ref } from 'vue'

const props = defineProps({ gate: { type: Object, required: true } })
const emit = defineEmits(['submit'])

const raiseTo = ref((props.gate.accrued_cny || 0) + 5)
</script>

<template>
  <el-card class="page-card" shadow="never">
    <template #header><b>COST · 成本护栏暂停闸</b></template>
    <p>已消耗 <b>¥{{ gate.accrued_cny }}</b> / 上限 ¥{{ gate.limit_cny }}
      <span class="muted">(¥5/篇护栏自动暂停)</span>
    </p>
    <p class="muted">{{ gate.hint }}</p>
    <div class="gate-actions" style="align-items:center">
      <span class="muted">新上限(元)</span>
      <el-input-number v-model="raiseTo" :step="0.5" :min="gate.accrued_cny || 0"
                       style="width:160px" />
      <el-button type="primary"
                 @click="emit('submit', { action: 'raise', raise_to: String(raiseTo ?? '') })">
        抬升上限并继续
      </el-button>
      <el-button type="danger" plain @click="emit('submit', { action: 'abort' })">终止任务</el-button>
    </div>
  </el-card>
</template>