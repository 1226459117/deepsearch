<script setup>
defineProps({ status: { type: String, default: '' } })

const MAP = {
  queued: ['info', '排队中'],
  running: ['primary', '执行中'],
  waiting_human: ['warning', '待人工确认'],
  done: ['success', '已完成'],
  failed: ['danger', '失败'],
}
</script>

<template>
  <el-tag :type="(MAP[status] || ['info', status])[0]">
    {{ (MAP[status] || ['info', status])[1] }}
  </el-tag>
</template>