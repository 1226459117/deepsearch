import { createRouter, createWebHashHistory } from 'vue-router'
import HomeView from './views/HomeView.vue'

// hash 模式:避免与后端遗留 HTML 路由(/threads/{tid}、/reports、/memory)冲突
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', component: HomeView },
    { path: '/threads/:tid', component: () => import('./views/ThreadView.vue'), props: true },
    { path: '/reports', component: () => import('./views/ReportsView.vue') },
    { path: '/memory', component: () => import('./views/MemoryView.vue') },
  ],
})

export default router