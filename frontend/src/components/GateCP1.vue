<script setup>
import { ref } from 'vue'

const props = defineProps({ gate: { type: Object, required: true } })
const emit = defineEmits(['submit'])

const outlineJson = ref(JSON.stringify(props.gate.outline ?? [], null, 2))
</script>

<template>
  <el-card class="page-card" shadow="never">
    <template #header><b>CP1 · 大纲确认闸(必停)</b></template>
    <p class="muted">{{ gate.hint }}</p>

    <template v-if="gate.directives?.length">
      <p><b>待消费指令:</b></p>
      <ul><li v-for="(d, i) in gate.directives" :key="i">{{ d.text }}</li></ul>
    </template>

    <el-collapse v-if="gate.conflict_notes?.length" style="margin-bottom:12px">
      <el-collapse-item :title="`数据分歧清单(${gate.conflict_notes.length} 条,merge_align 三级裁决仍未完全消除)`">
        <ul>
          <li v-for="(n, i) in gate.conflict_notes" :key="i">
            <b>{{ n.question }}</b> → 采信 {{ n.resolved_with }}({{ n.resolution_basis }})
          </li>
        </ul>
      </el-collapse-item>
    </el-collapse>

    <p><b>大纲编辑</b> <span class="muted">(JSON 列表;留空 = 采用 AI 大纲)</span></p>
    <el-input v-model="outlineJson" type="textarea" :rows="10"
              placeholder='[{"title": "第一章标题", "bullets": ["要点"]}]' />
    <div class="gate-actions">
      <el-button type="primary"
                 @click="emit('submit', { action: 'update', outline_json: outlineJson })">
        按编辑后大纲继续
      </el-button>
      <el-button @click="emit('submit', { action: 'approve' })">认可大纲,开始写作</el-button>
      <el-button type="danger" plain @click="emit('submit', { action: 'abort' })">终止任务</el-button>
    </div>
  </el-card>
</template>