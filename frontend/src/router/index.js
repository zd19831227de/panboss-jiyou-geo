import { createRouter, createWebHistory } from 'vue-router'
import BrandList from '../views/BrandList.vue'
import KeywordList from '../views/KeywordList.vue'
import MonitorCenter from '../views/MonitorCenter.vue'
import Dashboard from '../views/Dashboard.vue'
import CommandCenter from '../views/CommandCenter.vue'
import Operations from '../views/Operations.vue'
import Login from '../views/Login.vue'
import Settings from '../views/Settings.vue'

const routes = [
  { path: '/login', component: Login, meta: { requiresAuth: false } },
  { path: '/', redirect: '/brands' },
  { path: '/brands', component: BrandList, meta: { requiresAuth: true } },
  { path: '/keywords', component: KeywordList, meta: { requiresAuth: true } },
  { path: '/monitor', component: MonitorCenter, meta: { requiresAuth: true } },
  { path: '/dashboard', component: Dashboard, meta: { requiresAuth: true } },
  { path: '/command', redirect: '/command-center' },
  { path: '/command-center', component: CommandCenter, meta: { requiresAuth: true } },
  { path: '/operations', component: Operations, meta: { requiresAuth: true } },
  { path: '/settings', component: Settings, meta: { title: '系统设置', requiresAuth: true } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// Navigation guard
router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('geo_token')
  const requiresAuth = to.meta.requiresAuth !== false

  if (requiresAuth && !token) {
    next('/login')
  } else if (to.path === '/login' && token) {
    next('/')
  } else {
    next()
  }
})

export default router
