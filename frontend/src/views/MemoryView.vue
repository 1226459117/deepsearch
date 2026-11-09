<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiGet, apiPost } from '../api'

const snap = ref(null)
const loading = ref(true)
const styleContent = ref('')
const savingStyle = ref(false)
const rating = reactive({ domain: '', score: 0.8, note: '' })
const savingRating = ref(false)
const busyId = ref(null)

async function load() {
  loading.value = true
  try {
    snap.value = await apiGet('/api/memory')
    styleContent.value = snap.value.style_card || ''
  } catch (e) {
    ElMessage.error(`加载记忆数据失败:${e.message}`)
  } finally {
    loading.value = false
  }
}

async function decide(cand, action) {
  busyId.value = cand.id
  try {
    await apiPost(`/api/memory/${action}`, { candidate_id: cand.id })
    ElMessage.success(action === 'adopt' ? '已采纳' : '已拒绝')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    busyId.value = null
  }
}

async function saveStyle() {
  savingStyle.value = true
  try {
    await apiPost('/api/memory/style', { content: styleContent.value })
    ElMessage.success('风格卡已保存')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    savingStyle.value = false
  }
}

async function saveRating() {
  if (!rating.domain.trim()) {
    ElMessage.warning('请输入域名')
    return
  }
  savingRating.value = true
  try {
    await apiPost('/api/memory/rating', { ...rating })
    ElMessage.success('评分已保存')
    rating.domain = ''
    rating.note = ''
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    savingRating.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <h2 style="margin-top:0">记忆闭环管理</h2>
    <p class="muted">候选采纳 · 风格卡 · 数据源评分</p>

    <el-alert v-if="snap?.offline" type="error" :closable="false" style="margin-bottom:16px"
              title="L3 存储离线">
      <p>{{ snap.offline_err }} —— 本页功能需 PG/Milvus 自备实例可达;
        研报主链路不受影响(记忆注入自动降级)。网络恢复后刷新即可。</p>
    </el-alert>

    <template v-if="snap">
      <el-card class="page-card">
        <template #header><b>待审候选({{ snap.pending.length }})</b></template>
        <p class="muted">fact=事实归档(自动生效无需人审);profile=偏好画像(必须人审后才进拦截检索池)。</p>
        <el-table :data="snap.pending"
                  empty-text="无待审候选(CP2 人工改稿 diff、任务结束蒸馏都会在此产出候选)">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">
              <el-tag size="small">{{ row.kind }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="140">
            <template #default="{ row }">
              {{ row.source_type }}<br><span class="muted">{{ row.source_ref || '' }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="content" label="内容" min-width="240" />
          <el-table-column label="操作" width="160">
            <template #default="{ row }">
              <el-button size="small" type="primary" :loading="busyId === row.id"
                         @click="decide(row, 'adopt')">采纳</el-button>
              <el-button size="small" type="danger" plain :disabled="busyId === row.id"
                         @click="decide(row, 'reject')">拒绝</el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-collapse style="margin-top:12px">
          <el-collapse-item :title="`已采纳 ${snap.adopted.length} / 已拒绝 ${snap.rejected.length}`">
            <h4>已采纳</h4>
            <el-table :data="snap.adopted" size="small" empty-text="暂无">
              <el-table-column prop="id" label="ID" width="70" />
              <el-table-column prop="kind" label="类型" width="100" />
              <el-table-column prop="content" label="内容" />
            </el-table>
            <h4>已拒绝</h4>
            <el-table :data="snap.rejected" size="small" empty-text="暂无">
              <el-table-column prop="id" label="ID" width="70" />
              <el-table-column prop="kind" label="类型" width="100" />
              <el-table-column prop="content" label="内容" />
            </el-table>
          </el-collapse-item>
        </el-collapse>
      </el-card>

      <el-card class="page-card">
        <template #header><b>风格卡</b>
          <span class="muted" style="margin-left:8px">注入 plan/chapter 写作提示</span>
        </template>
        <el-input v-model="styleContent" type="textarea" :rows="3"
                  placeholder="例:结论先行,少用形容词;数据必须带年份与来源;每章至少一张图表说明" />
        <div style="margin-top:8px">
          <el-button type="primary" :loading="savingStyle" @click="saveStyle">保存风格卡</el-button>
        </div>
      </el-card>

      <el-card class="page-card">
        <template #header><b>数据源可靠性</b>
          <span class="muted" style="margin-left:8px">域名级轻评分,低分域名在 merge 阶段降权</span>
        </template>
        <el-table :data="snap.ratings" empty-text="暂无评分,merge 阶段默认 0.5">
          <el-table-column prop="domain" label="域名" min-width="200" />
          <el-table-column prop="score" label="score" width="100" />
          <el-table-column label="备注" min-width="200">
            <template #default="{ row }"><span class="muted">{{ row.note || '' }}</span></template>
          </el-table-column>
        </el-table>
        <el-form inline style="margin-top:12px" @submit.prevent="saveRating">
          <el-form-item label="域名">
            <el-input v-model="rating.domain" placeholder="revenue.caixin.com" style="width:220px" />
          </el-form-item>
          <el-form-item label="score(0~1)">
            <el-input-number v-model="rating.score" :min="0" :max="1" :step="0.05"
                             style="width:130px" />
          </el-form-item>
          <el-form-item label="备注">
            <el-input v-model="rating.note" placeholder="如:口径严谨" style="width:200px" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="savingRating" @click="saveRating">提交评分</el-button>
          </el-form-item>
        </el-form>
      </el-card>
    </template>
  </div>
</template>