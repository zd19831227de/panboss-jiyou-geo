<template>
  <div v-if="!isLoginPage" class="app-container">
    <el-aside width="216px" class="sidebar">
      <div class="logo">
        <div class="logo-mark">
          <svg width="26" height="26" viewBox="0 0 48 48" fill="none">
            <path d="M24 10L24 38M16 16L24 10L32 16" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M14 28L24 38L34 28" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <div class="logo-text">
          <span class="logo-title">GEO运营系统</span>
          <span class="logo-sub">AI 搜索优化平台</span>
        </div>
      </div>
      <el-menu
        :default-active="activeMenu"
        class="sidebar-menu"
        router
      >
        <el-menu-item index="/brands">
          <el-icon><Shop /></el-icon>
          <span>品牌管理</span>
        </el-menu-item>
        <el-menu-item index="/keywords">
          <el-icon><Document /></el-icon>
          <span>关键词库</span>
        </el-menu-item>
        <el-menu-item index="/monitor">
          <el-icon><Monitor /></el-icon>
          <span>AI监测中心</span>
        </el-menu-item>
        <el-menu-item index="/dashboard">
          <el-icon><DataLine /></el-icon>
          <span>数据中心</span>
        </el-menu-item>
        <el-menu-item index="/command-center">
          <el-icon><TrendCharts /></el-icon>
          <span>运营大屏</span>
        </el-menu-item>
        <el-menu-item index="/operations">
          <el-icon><Operation /></el-icon>
          <span>运营中台</span>
        </el-menu-item>
        <el-menu-item index="/settings">
          <el-icon><Setting /></el-icon>
          <span>系统设置</span>
        </el-menu-item>
      </el-menu>
      <div class="sidebar-footer">v1.1.0</div>
    </el-aside>
    <el-container>
      <el-header class="header" height="60px">
        <div class="header-left">
          <h1 class="page-title">{{ pageTitle }}</h1>
        </div>
        <div class="header-right">
          <div class="user-chip" v-if="auth.user">
            <div class="user-avatar">{{ auth.user.username?.slice(0, 1)?.toUpperCase() }}</div>
            <span class="user-name">{{ auth.user.username }}</span>
          </div>
          <el-button size="small" @click="handleLogout" v-if="auth.isAuthenticated">退出</el-button>
        </div>
      </el-header>
      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>
  </div>
  <router-view v-else />
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Shop, Document, Monitor, DataLine, Setting, TrendCharts, Operation } from '@element-plus/icons-vue'
import { useAuthStore } from './stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const activeMenu = computed(() => route.path)
const isLoginPage = computed(() => route.path === '/login')

const titleMap = {
  '/brands': '品牌管理',
  '/keywords': '关键词库',
  '/monitor': 'AI监测中心',
  '/dashboard': '数据中心',
  '/command-center': '运营大屏',
  '/operations': '运营中台',
  '/settings': '系统设置',
}
const pageTitle = computed(() => titleMap[route.path] || 'GEO运营系统')

const handleLogout = () => {
  auth.logout()
  router.push('/login')
}

onMounted(() => {
  if (route.path !== '/login') {
    auth.checkAuth()
  }
})
</script>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Helvetica Neue', Helvetica, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', Arial, sans-serif; }

.app-container { height: 100vh; display: flex; }

/* ---------- 侧边栏 ---------- */
.sidebar {
  background: var(--brand-sidebar-bg);
  color: #fff;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  box-shadow: 2px 0 12px rgba(10, 18, 40, 0.25);
  z-index: 10;
}
.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
}
.logo-mark {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  background: var(--brand-gradient);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px rgba(76, 111, 255, 0.45);
  flex-shrink: 0;
}
.logo-text { display: flex; flex-direction: column; line-height: 1.25; }
.logo-title { font-size: 15px; font-weight: 700; color: #fff; letter-spacing: 0.5px; }
.logo-sub { font-size: 11px; color: rgba(255, 255, 255, 0.45); letter-spacing: 1px; }

.sidebar-menu {
  border: none;
  background: transparent;
  flex: 1;
  padding: 10px 10px;
  --el-menu-text-color: rgba(255, 255, 255, 0.62);
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.06);
  --el-menu-bg-color: transparent;
  --el-menu-active-color: #fff;
}
.sidebar-menu .el-menu-item {
  color: rgba(255, 255, 255, 0.62);
  border-radius: 8px;
  margin-bottom: 4px;
  height: 44px;
  line-height: 44px;
  transition: all 0.2s ease;
}
.sidebar-menu .el-menu-item:hover {
  background: rgba(255, 255, 255, 0.07);
  color: #fff;
}
.sidebar-menu .el-menu-item.is-active {
  background: var(--brand-sidebar-active);
  color: #fff;
  font-weight: 600;
  box-shadow: 0 4px 12px rgba(76, 111, 255, 0.4);
}
.sidebar-footer {
  padding: 14px;
  text-align: center;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.3);
  border-top: 1px solid rgba(255, 255, 255, 0.07);
}

/* ---------- 顶栏 ---------- */
.header {
  background-color: #fff;
  box-shadow: 0 1px 4px rgba(16, 28, 61, 0.06);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  z-index: 5;
}
.page-title { font-size: 17px; font-weight: 600; color: var(--el-text-color-primary); }
.header-right { display: flex; align-items: center; gap: 14px; }
.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px 4px 4px;
  background: var(--el-fill-color-light);
  border-radius: 20px;
}
.user-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--brand-gradient);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.user-name { color: var(--el-text-color-regular); font-size: 13px; font-weight: 500; }

.main-content { background-color: var(--brand-bg); padding: 20px; flex: 1; overflow-y: auto; }
</style>
