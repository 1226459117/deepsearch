<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiGet } from '../api'

const root = ref('')
const dirs = ref([])
const loading = ref(true)
const previewUrl = ref('')
const previewVisible = ref(false)

async function load() {
  try {
    const data = await apiGet('/api/reports')
    root.value = data.root
    dirs.value = data.dirs
  } catch (e) {
    ElMessage.error(`加载产物列表失败:${e.message}`)
  } finally {
    loading.value = false
  }
}

function preview(dirName, rel) {
  previewUrl.value = `/files/${dirName}/${rel}?inline=1`
  previewVisible.value = true
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <h2 style="margin-top:0">研报产物浏览</h2>
    <p class="muted">目录:{{ root }} · MD 为唯一权威源,HTML/PDF 为渲染产物</p>

    <el-empty v-if="!dirs.length"
              description="还没有产物。提交一个研报选题跑完整流水线后,这里会出现产物目录。" />

    <el-card v-for="d in dirs" :key="d.name" class="page-card">
      <template #header>
        <el-link type="primary" :href="`/files/${d.name}/report.md`"><b>{{ d.name }}</b></el-link>
      </template>
      <ul v-if="d.rels.length" style="margin:0; padding-left:20px;">
        <li v-for="rel in d.rels" :key="rel" style="line-height:2">
          <el-link :href="`/files/${d.name}/${rel}`">{{ rel }}</el-link>
          <el-button v-if="rel.endsWith('.html')" size="small" text type="primary"
                     @click="preview(d.name, rel)">在线预览</el-button>
        </li>
      </ul>
      <span v-else class="muted">(空目录)</span>
    </el-card>

    <el-dialog v-model="previewVisible" title="报告预览" width="80%" top="5vh"
               destroy-on-close>
      <iframe :src="previewUrl" style="width:100%; height:72vh; border:none;" />
    </el-dialog>
  </div>
</template>