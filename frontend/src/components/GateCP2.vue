<script setup>
import { reactive, ref } from 'vue'

const props = defineProps({ gate: { type: Object, required: true } })
const emit = defineEmits(['submit'])

const chapters = props.gate.chapters || {}
const findings = props.gate.review_findings || {}
const edits = reactive(Object.fromEntries(Object.entries(chapters)))
const resolutionsJson = ref('')
const openChapters = ref(Object.keys(findings).filter((cid) => findings[cid]))

function submit(action) {
  const form = { action }
  if (action === 'submit') {
    for (const [cid, text] of Object.entries(edits)) form[`edit_${cid}`] = text
    form.resolutions_json = resolutionsJson.value
  }
  emit('submit', form)
}
</script>

<template>
  <el-card class="page-card" shadow="never">
    <template #header><b>CP2 · 成稿确认闸(必停)</b></template>
    <p class="muted">{{ gate.hint }}</p>

    <template v-if="gate.directives?.length">
      <p><b>待消费指令:</b></p>
      <ul><li v-for="(d, i) in gate.directives" :key="i">{{ d.text }}</li></ul>
    </template>

    <el-collapse v-if="Object.values(findings).some(Boolean)" style="margin-bottom:12px">
      <el-collapse-item title="审稿遗留问题(定向重写后仍未消除)" name="findings">
        <ul>
          <li v-for="(note, cid) in findings" :key="cid">
            <template v-if="note"><b>{{ cid }}</b>:{{ note }}</template>
          </li>
        </ul>
      </el-collapse-item>
    </el-collapse>

    <el-collapse v-if="gate.conflict_notes?.length" style="margin-bottom:12px">
      <el-collapse-item :title="`系统裁决的数据分歧(${gate.conflict_notes.length} 条,可人工改判)`">
        <ul>
          <li v-for="(n, i) in gate.conflict_notes" :key="i">
            <b>{{ n.question }}</b> → 已采信 {{ n.resolved_with }}({{ n.resolution_basis }})
          </li>
        </ul>
      </el-collapse-item>
    </el-collapse>

    <p><b>逐章改稿</b>
      <span class="muted">(内容保持与原稿一致 = 不改动;修改过的章将直接采用人工稿)</span>
    </p>
    <el-collapse v-model="openChapters">
      <el-collapse-item v-for="(md, cid) in chapters" :key="cid" :name="cid">
        <template #title>
          {{ cid }}({{ md.length }} 字符)
          <el-tag v-if="findings[cid]" type="warning" size="small" style="margin-left:8px">
            {{ findings[cid] }}
          </el-tag>
        </template>
        <el-input v-model="edits[cid]" type="textarea" :rows="8" />
      </el-collapse-item>
    </el-collapse>

    <p style="margin-top:12px"><b>分歧人工定夺</b> <span class="muted">(JSON 列表,可空)</span></p>
    <el-input v-model="resolutionsJson" type="textarea" :rows="3"
              placeholder='[{"question": "装机量口径?", "decision": "以协会公告为准"}]' />

    <div class="gate-actions">
      <el-button type="primary" @click="submit('submit')">提交改稿并装配导出</el-button>
      <el-button @click="submit('approve')">全部认可 AI 稿</el-button>
      <el-button type="danger" plain @click="submit('abort')">终止(草稿保留)</el-button>
    </div>
  </el-card>
</template>