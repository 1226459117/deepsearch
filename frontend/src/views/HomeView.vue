<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiGet, apiPost } from '../api'
import StatusTag from '../components/StatusTag.vue'

const router = useRouter()
const topic = ref('')
const route = ref('auto')
const submitting = ref(false)
const jobs = ref([])
const counts = ref({})
const now = ref(0)
const loading = ref(false)
let timer = null

const ROUTE_LABELS = { '': '自动', chat: '闲聊', factual: '事实题', report: '研报', steering: '指令' }
const ACTIVE = ['queued', 'running', 'waiting_human']

async function load() {
  try {
    const data = await apiGet('/api/overview')
    jobs.value = data.jobs
    counts.value = data.counts
    now.value = data.now
  } catch (e) {
    ElMessage.error(`加载任务列表失败:${e.message}`)
  } finally {
    loading.value = false
  }
}

async function submit() {
  if (!topic.value.trim()) {
    ElMessage.warning('请输入选题或问题')
    return
  }
  submitting.value = true
  try {
    const data = await apiPost('/api/tasks', {
      topic: topic.value,
      route: route.value === 'auto' ? '' : route.value,
    })
    topic.value = ''
    router.push(`/threads/${data.thread_id}`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    submitting.value = false
  }
}

async function cancel(job) {
  try {
    await ElMessageBox.confirm(
      'running 中的任务在本轮超步结束后停下(取消点仅在节点边界生效)。确认取消?',
      '取消任务', { type: 'warning', confirmButtonText: '取消任务', cancelButtonText: '再想想' },
    )
  } catch {
    return
  }
  try {
    await apiPost(`/api/jobs/${job.id}/cancel`)
    ElMessage.success('已取消')
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function retry(job) {
  try {
    const data = await apiPost(`/api/jobs/${job.id}/retry`)
    router.push(`/threads/${data.thread_id}`)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function ago(ts) {
  const s = Math.max(0, Math.round(now.value - ts))
  if (s < 60) return `${s}s 前`
  if (s < 3600) return `${Math.floor(s / 60)}m 前`
  return `${Math.floor(s / 3600)}h 前`
}

onMounted(() => {
  loading.value = true
  load()
  timer = setInterval(load, 3000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <el-card class="page-card">
    <template #header><b>新建任务</b>
      <span class="muted" style="margin-left:8px">一句话即可,系统自动分诊:闲聊 · 小型事实题 · 深度研报</span>
    </template>
    <el-form label-position="top" @submit.prevent="submit">
      <el-form-item label="选题 / 提问">
        <el-input v-model="topic" type="textarea" :rows="3"
                  placeholder="例:光伏行业前景分析 / 600519 最新毛利率 / 今天天气怎么样" />
      </el-form-item>
      <div style="display:flex; gap:12px; align-items:center;">
        <span class="muted">分诊档位</span>
        <el-select v-model="route" style="width:260px">
          <el-option label="自动分诊(推荐)" value="auto" />
          <el-option label="强制深度研报(14 节点流水线)" value="report" />
          <el-option label="强制闲聊" value="chat" />
          <el-option label="强制小型事实题" value="factual" />
        </el-select>
        <el-button type="primary" :loading="submitting" @click="submit">
          提交任务
        </el-button>
      </div>
    </el-form>
  </el-card>

  <el-card v-loading="loading" class="page-card">
    <template #header>
      <b>最近任务</b>
      <span style="margin-left:12px">
        <el-tag v-for="(n, st) in counts" :key="st" size="small" style="margin-right:6px"
                :type="st === 'failed' ? 'danger' : st === 'done' ? 'success' : st === 'waiting_human' ? 'warning' : 'info'">
          {{ st }} {{ n }}
        </el-tag>
      </span>
    </template>
    <el-table :data="jobs" empty-text="暂无任务" style="width:100%">
      <el-table-column label="thread" width="150">
        <template #default="{ row }">
          <el-link type="primary" @click="router.push(`/threads/${row.thread_id}`)">
            {{ row.thread_id }}
          </el-link>
        </template>
      </el-table-column>
      <el-table-column prop="input" label="输入" min-width="220" show-overflow-tooltip />
      <el-table-column label="档位" width="90">
        <template #default="{ row }">{{ ROUTE_LABELS[row.route] ?? row.route }}</template>
      </el-table-column>
      <el-table-column label="状态" width="130">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="更新" width="90">
        <template #default="{ row }"><span class="muted">{{ ago(row.updated_at) }}</span></template>
      </el-table-column>
      <el-table-column label="操作" width="100">
        <template #default="{ row }">
          <el-button v-if="ACTIVE.includes(row.status)" size="small" type="danger" plain
                     @click="cancel(row)">取消</el-button>
          <el-button v-else-if="row.status === 'failed'" size="small" @click="retry(row)">
            重试
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>