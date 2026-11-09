<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { apiGet } from './api'

const route = useRoute()
const version = ref('')

const activeMenu = computed(() =>
  route.path.startsWith('/threads') ? '/' : route.path,
)

onMounted(async () => {
  try {
    const h = await apiGet('/healthz')
    version.value = h.version
  } catch {
    /* 后端未就绪时不阻塞页面 */
  }
})
</script>

<template>
  <el-container class="layout">
    <el-header class="header" height="60px">
      <div class="brand">
        深度研报智能体
        <span v-if="version" class="muted">v{{ version }}</span>
      </div>
      <el-menu mode="horizontal" :default-active="activeMenu" router :ellipsis="false"
               class="nav">
        <el-menu-item index="/">任务</el-menu-item>
        <el-menu-item index="/reports">研报产物</el-menu-item>
        <el-menu-item index="/memory">记忆管理</el-menu-item>
      </el-menu>
    </el-header>
    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100vh;
}

.header {
  display: flex;
  align-items: center;
  gap: 24px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.brand {
  font-size: 18px;
  font-weight: 600;
  white-space: nowrap;
}

.nav {
  flex: 1;
  border-bottom: none;
}

.main {
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
}
</style>