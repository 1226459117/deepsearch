<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { apiGet, apiPost, apiUpload } from '../api'
import StatusTag from '../components/StatusTag.vue'
import GateCP1 from '../components/GateCP1.vue'
import GateCP2 from '../components/GateCP2.vue'
import GateCost from '../components/GateCost.vue'

const props = defineProps({ tid: { type: String, required: true } })
const router = useRouter()

const job = ref(null)
const progress = ref(null)
const loadError = ref('')
const events = ref([])
const evlogEl = ref(null)
const steerText = ref('')
const steering = ref(false)
const rerouteRoute = ref('report')
const gateSubmitting = ref(false)
let es = null
let timer = null

const ACTIVE = ['queued', 'running', 'waiting_human']
const isActive = computed(() => job.value && ACTIVE.includes(job.value.status))
const costPct = computed(() => {
  const p = progress.value
  if (!p || !p.cost_limit_cny) return 0
  return Math.min(100, Math.round((p.cost_cny / p.cost_limit_cny) * 100))
})

function addEvent(text, dim = false) {
  events.value.push({ time: new Date().toLocaleTimeString(), text, dim })
  if (events.value.length > 500) events.value.splice(0, events.value.length - 500)
}

async function load() {
  try {
    const data = await apiGet(`/api/threads/${props.tid}`)
    job.value = data.job
    progress.value = data.progress
    loadError.value = ''
  } catch (e) {
    loadError.value = e.message
  }
}

function closeEvents() {
  if (es) {
    es.close()
    es = null
  }
}

function openEvents() {
  closeEvents()
  const source = new EventSource(`/threads/${props.tid}/events`)
  es = source
  source.addEventListener('node', (e) => {
    const d = JSON.parse(e.data)
    const p = d.progress || {}
    addEvent(`[${d.node}] ${p.stage || ''} · ¥${p.cost_cny ?? 0}`)
    if (progress.value) Object.assign(progress.value, p)
  })
  source.addEventListener('gate', (e) => {
    const d = JSON.parse(e.data)
    addEvent(`⏸ 等待人工确认:${d.gate?.gate || ''}`, true)
    load()
  })
  source.addEventListener('resumed', () => {
    addEvent('▶ 决策已提交,任务恢复执行')
    load()
  })
  source.addEventListener('steered', (e) => {
    addEvent(`指令已投递:${JSON.parse(e.data).text || ''}`)
    load()
  })
  source.addEventListener('done', (e) => {
    const d = JSON.parse(e.data)
    addEvent(`✔ 完成:${d.progress?.stage || ''}`, true)
    load()
    closeEvents()
  })
  source.addEventListener('cancelled', () => {
    addEvent('任务已取消', true)
    load()
    closeEvents()
  })
  source.addEventListener('error', (e) => {
    if (!e.data) return // 连接级错误交给 onerror
    let msg = '执行失败'
    try {
      msg += `:${JSON.parse(e.data).error.slice(0, 120)}`
    } catch { /* 错误体非 JSON */ }
    addEvent(`✖ ${msg}`, true)
    load()
    closeEvents()
  })
  source.onerror = () => {
    // 终态后后端主动断流,浏览器默认重连没有意义;统一关闭改由轮询兜底
    addEvent('(SSE 断开,进度改由 3s 轮询刷新)', true)
    closeEvents()
  }
}

async function submitGate(form) {
  if (gateSubmitting.value) return
  if (form.action === 'abort') {
    try {
      await ElMessageBox.confirm('确认终止该任务?', '终止任务', { type: 'warning' })
    } catch {
      return
    }
  }
  gateSubmitting.value = true
  try {
    await apiPost(`/api/threads/${props.tid}/gate`, form)
    ElMessage.success('决策已提交')
    await load()
    if (!es) openEvents()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    gateSubmitting.value = false
  }
}

async function steer() {
  if (!steerText.value.trim()) {
    ElMessage.warning('请输入指令内容')
    return
  }
  steering.value = true
  try {
    await apiPost(`/api/threads/${props.tid}/steer`, { text: steerText.value })
    ElMessage.success('指令已入队,将在下一网关消费')
    steerText.value = ''
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    steering.value = false
  }
}

async function doUpload(options) {
  try {
    const data = await apiUpload(`/api/threads/${props.tid}/upload`, options.file)
    ElMessage.success(`解析入池成功:${data.doc_id}`)
    load()
  } catch (e) {
    ElMessage.error(e.message)
    throw e
  }
}

async function cancelJob() {
  try {
    await ElMessageBox.confirm(
      'running 中的任务在本轮超步结束后停下(取消点仅在节点边界生效)。确认取消?',
      '取消任务', { type: 'warning', confirmButtonText: '取消任务', cancelButtonText: '再想想' },
    )
  } catch {
    return
  }
  try {
    await apiPost(`/api/jobs/${job.value.id}/cancel`)
    ElMessage.success('已取消')
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function retryJob() {
  try {
    const data = await apiPost(`/api/jobs/${job.value.id}/retry`)
    router.push(`/threads/${data.thread_id}`)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function reroute() {
  try {
    await apiPost(`/api/threads/${props.tid}/reroute`, { route: rerouteRoute.value })
    ElMessage.success('已按选定档位重新入队')
    load()
    if (!es) openEvents()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

watch(() => props.tid, () => {
  events.value = []
  job.value = null
  progress.value = null
  load()
  openEvents()
})

watch(() => events.value.length, async () => {
  await nextTick()
  if (evlogEl.value) evlogEl.value.scrollTop = evlogEl.value.scrollHeight
})

onMounted(() => {
  load()
  openEvents()
  timer = setInterval(load, 3000)
})

onUnmounted(() => {
  closeEvents()
  clearInterval(timer)
})
</script>

<template>
  <div>
    <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px;">
      <h2 style="margin:0">任务详情</h2>
      <StatusTag v-if="progress" :status="progress.status" />
      <span class="muted">thread = {{ tid }}</span>
      <el-button size="small" @click="router.push('/')">返回任务列表</el-button>
    </div>
    <p v-if="job" class="muted">输入:{{ job.input || '(续单)' }}</p>
    <el-alert v-if="loadError" :title="loadError" type="error" :closable="false"
              style="margin-bottom:16px" />

    <el-card v-if="progress" class="page-card">
      <template #header><b>进度面板</b><span class="muted"> · 每 3s 刷新</span></template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="阶段">{{ progress.stage }}</el-descriptions-item>
        <el-descriptions-item label="状态"><StatusTag :status="progress.status" /></el-descriptions-item>
        <el-descriptions-item label="大纲 / 完成章">
          {{ progress.outline_n }} / {{ progress.chapters_done }}
        </el-descriptions-item>
        <el-descriptions-item label="调研问题 · 素材">
          {{ progress.plan_n }} · {{ progress.sources }}
        </el-descriptions-item>
        <el-descriptions-item label="成本(已用/上限)">
          ¥{{ progress.cost_cny }} / ¥{{ progress.cost_limit_cny }}
          <el-progress v-if="progress.cost_limit_cny" :percentage="costPct" :stroke-width="6"
                       style="width:140px; display:inline-flex; margin-left:8px; vertical-align:middle" />
        </el-descriptions-item>
        <el-descriptions-item label="检索次数">{{ progress.search_used }}</el-descriptions-item>
        <el-descriptions-item v-if="progress.dirty?.length" label="待重写章" :span="2">
          {{ progress.dirty.join(', ') }}(审稿 {{ progress.review_rounds }} 轮)
        </el-descriptions-item>
        <el-descriptions-item v-if="progress.directives_open > 0" label="未消费指令" :span="2">
          {{ progress.directives_open }} 条,将在下一网关消费
        </el-descriptions-item>
      </el-descriptions>

      <div v-if="progress.status === 'waiting_human' && job?.gate" style="margin-top:16px">
        <GateCP1 v-if="job.gate.gate === 'CP1'"
                 :key="`cp1-${job.id}-${job.updated_at}`" :gate="job.gate" @submit="submitGate" />
        <GateCP2 v-else-if="job.gate.gate === 'CP2'"
                 :key="`cp2-${job.id}-${job.updated_at}`" :gate="job.gate" @submit="submitGate" />
        <GateCost v-else-if="job.gate.gate === 'COST'"
                  :key="`cost-${job.id}-${job.updated_at}`" :gate="job.gate" @submit="submitGate" />
      </div>

      <template v-else-if="progress.status === 'done'">
        <div v-if="progress.report_name" style="margin-top:16px">
          <h3 style="margin:8px 0">产物</h3>
          <el-space wrap>
            <el-link type="primary" :href="`/files/${progress.report_name}/report.md`">
              report.md(权威源)
            </el-link>
            <el-link type="primary" target="_blank"
                     :href="`/files/${progress.report_name}/report.html?inline=1`">
              report.html
            </el-link>
            <el-link type="primary" :href="`/files/${progress.report_name}/report.pdf`">
              report.pdf(若已生成)
            </el-link>
            <el-link type="info" @click="router.push('/reports')">全部产物浏览</el-link>
          </el-space>
        </div>
        <div v-if="progress.answer">
          <h3 style="margin:8px 0">回复</h3>
          <div class="pre-wrap">{{ progress.answer }}</div>
        </div>
        <el-card v-if="progress.low_conf" shadow="never" style="margin-top:16px">
          <p><b>分诊复核</b>
            <span class="muted">(置信度 {{ Number(progress.confidence).toFixed(2) }} 偏低,已按保守档处理)</span>
          </p>
          <p class="muted">如档位不符,用原输入按选定档位重新执行:</p>
          <div style="display:flex; gap:8px; align-items:center;">
            <el-select v-model="rerouteRoute" style="width:260px">
              <el-option label="研报选题(深度流水线)" value="report" />
              <el-option label="小型事实题(快查直答)" value="factual" />
              <el-option label="闲聊/通用问答" value="chat" />
            </el-select>
            <el-button @click="reroute">改档重跑</el-button>
          </div>
        </el-card>
      </template>

      <template v-else-if="progress.status === 'failed'">
        <h3 style="margin:8px 0">错误</h3>
        <div class="pre-wrap">{{ progress.error }}</div>
        <el-button @click="retryJob">重试(全新 thread 同输入重跑)</el-button>
      </template>

      <div v-if="isActive" style="margin-top:16px">
        <el-button type="danger" plain @click="cancelJob">取消任务</el-button>
        <span class="muted" style="margin-left:8px">
          running 中的任务在本轮超步结束后停下(取消点仅在节点边界生效)
        </span>
      </div>
    </el-card>

    <el-card class="page-card">
      <template #header><b>执行事件流(SSE)</b></template>
      <div ref="evlogEl" class="evlog">
        <div v-if="!events.length" class="dim">连接中…</div>
        <div v-for="(ev, i) in events" :key="i" :class="{ dim: ev.dim }">
          {{ ev.time }}&nbsp;&nbsp;{{ ev.text }}
        </div>
      </div>
    </el-card>

    <el-card class="page-card">
      <template #header>
        <b>向本任务投递指令</b>
        <span class="muted" style="margin-left:8px">task_steering,不打断当前执行,下一网关消费</span>
      </template>
      <el-input v-model="steerText" type="textarea" :rows="2"
                placeholder="例:第二章多画两张图 / 结论保守一点" />
      <div style="margin-top:8px">
        <el-button type="primary" :loading="steering" @click="steer">投递指令</el-button>
      </div>
    </el-card>

    <el-card class="page-card">
      <template #header>
        <b>上传私有资料</b>
        <span class="muted" style="margin-left:8px">入本任务检索池,PDF/TXT/MD,≤40MB</span>
      </template>
      <el-upload :http-request="doUpload" accept=".pdf,.txt,.md,.markdown" drag>
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">拖拽文件到此处,或 <em>点击选择</em></div>
      </el-upload>
      <p class="muted">
        PDF 优先走 MinerU(需配 MINERU_API_URL),否则 pymupdf 抽文本;入池后参与章节素材检索。
      </p>
    </el-card>
  </div>
</template>