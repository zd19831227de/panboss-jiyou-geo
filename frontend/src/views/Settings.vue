<template>
  <div class="settings">
    <!-- 大模型配置 -->
    <el-card class="settings-card">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">大模型设置 — AI大脑</span>
          <el-button size="small" @click="loadLlm" :loading="loadingLlm">刷新</el-button>
        </div>
      </template>

      <el-alert
        title="未配置接口密钥时，关键词生产、内容生产、AI报告不会伪装生成。"
        type="warning"
        :closable="false"
        show-icon
        class="section-alert"
      />

      <el-table :data="llmProviders" stripe>
        <el-table-column prop="display_name" label="模型平台" width="120" />
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch v-model="row.enabled" @change="saveLlmProvider(row)" />
          </template>
        </el-table-column>
        <el-table-column label="接口地址" min-width="260">
          <template #default="{ row }">
            <el-input v-model="row.base_url" size="small" @change="saveLlmProvider(row)" />
          </template>
        </el-table-column>
        <el-table-column label="默认模型" width="170">
          <template #default="{ row }">
            <el-input v-model="row.default_model" size="small" @change="saveLlmProvider(row)" />
          </template>
        </el-table-column>
        <el-table-column label="接口密钥" width="230">
          <template #default="{ row }">
            <el-input
              v-model="row.api_key_input"
              size="small"
              show-password
              :placeholder="row.api_key_configured ? row.api_key_masked : '填写后本地保存'"
              @change="saveLlmProvider(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="价格/千令牌" width="180">
          <template #default="{ row }">
            <div class="price-inputs">
              <el-input-number v-model="row.prompt_price_per_1k" :min="0" :precision="4" size="small" @change="saveLlmProvider(row)" />
              <el-input-number v-model="row.completion_price_per_1k" :min="0" :precision="4" size="small" @change="saveLlmProvider(row)" />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="150">
          <template #default="{ row }">
            <el-tag :type="llmStatusTag(row.last_test_status)" size="small">{{ llmStatusLabel(row.last_test_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" :loading="testingLlm === row.provider" @click="testLlm(row)">测试连接</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="usage-grid">
        <div class="usage-item">
          <span>今日调用</span>
          <b>{{ llmUsage.today?.calls || 0 }}</b>
        </div>
        <div class="usage-item">
          <span>今日令牌用量</span>
          <b>{{ llmUsage.today?.tokens || 0 }}</b>
        </div>
        <div class="usage-item">
          <span>本月令牌用量</span>
          <b>{{ llmUsage.month?.tokens || 0 }}</b>
        </div>
        <div class="usage-item">
          <span>估算费用</span>
          <b>{{ formatCost(llmUsage.month?.cost) }}</b>
        </div>
      </div>
    </el-card>

    <!-- Capsolver配置 -->
    <el-card class="settings-card">
      <template #header><span class="card-title">验证码自动解决 — Capsolver</span></template>
      <el-form label-width="160px">
        <el-form-item label="状态">
          <el-tag :type="captchaStatus.paid_available ? 'success' : 'danger'" size="default">
            {{ captchaStatus.paid_available ? '✅ 已配置' : '❌ 未配置' }}
          </el-tag>
          <span class="form-hint">{{ captchaStatus.notes }}</span>
        </el-form-item>
        <el-form-item label="服务商"><span>{{ captchaStatus.provider || '无' }}</span></el-form-item>
        <el-form-item label="支持的验证码" v-if="captchaStatus.supported_types?.length">
          <el-tag v-for="t in captchaStatus.supported_types" :key="t" size="small" style="margin-right:6px;">{{ t }}</el-tag>
        </el-form-item>
        <el-form-item label="降级模式">
          <el-tag :type="captchaStatus.fallback_mode === 'auto+manual' ? 'success' : 'warning'" size="small">
            {{ fallbackModeLabel(captchaStatus.fallback_mode) }}
          </el-tag>
        </el-form-item>
        <el-form-item label="计费参考">
          <div class="price-tags">
            <el-tag size="small" type="info">谷歌验证码 v2：约 $0.5/千次</el-tag>
            <el-tag size="small" type="info">hCaptcha：约 $0.5/千次</el-tag>
            <el-tag size="small" type="info">Turnstile：约 $1/千次</el-tag>
          </div>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 引擎状态 -->
    <el-card class="settings-card">
      <template #header><span class="card-title">监测引擎状态</span></template>
      <el-table :data="engineStatus" stripe>
        <el-table-column prop="name" label="引擎" width="100" />
        <el-table-column prop="mode" label="工作模式" width="120" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="sessionTagType(row.status)" size="small">{{ sessionLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="port" label="端口" width="80" />
        <el-table-column prop="profile_dir" label="登录态目录" min-width="180" show-overflow-tooltip />
        <el-table-column prop="notes" label="备注" min-width="200" show-overflow-tooltip />
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <div v-if="row.platform !== 'web'" class="action-buttons">
              <el-button size="small" @click="openLogin(row.platform)">打开登录</el-button>
              <el-button size="small" type="primary" :loading="testingPlatform === row.platform" @click="selfTest(row.platform)">运行自检</el-button>
            </div>
            <span v-else class="muted">无需登录</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 系统信息 -->
    <el-card class="settings-card">
      <template #header><span class="card-title">系统信息</span></template>
      <el-form label-width="160px">
        <el-form-item label="后端状态">
          <el-tag :type="serviceTagType(backendStatus)">{{ serviceLabel(backendStatus) }}</el-tag>
        </el-form-item>
        <el-form-item label="工作进程状态">
          <el-tag :type="serviceTagType(workerStatus)">{{ serviceLabel(workerStatus) }}</el-tag>
        </el-form-item>
        <el-form-item label="前端版本">Vue 3 + Element Plus + Vite 5</el-form-item>
        <el-form-item label="后端版本">FastAPI + SQLite (v2.0)</el-form-item>
        <el-form-item label="数据库">
          <span>{{ dbSize }}</span>
        </el-form-item>
        <el-form-item label="操作">
          <el-button size="small" @click="refreshAll" :loading="loadingStatus">刷新状态</el-button>
          <el-button size="small" type="warning" @click="runScheduler('daily')">运行每日扫描</el-button>
          <el-button size="small" type="danger" @click="runScheduler('weekly')">运行全量扫描</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 自动监测循环 -->
    <el-card class="settings-card">
      <template #header><span class="card-title">自动监测循环</span></template>
      <el-alert
        title="开启后系统每 N 小时自动执行一次增量扫描和登录态检查，无需人工点击。发布仍需人工确认。"
        type="info"
        :closable="false"
        show-icon
        class="section-alert"
      />
      <el-form label-width="160px">
        <el-form-item label="自动监测">
          <el-switch v-model="autoMonitorOn" @change="toggleAutoMonitor" :loading="autoMonitorLoading" />
          <span class="form-hint">{{ autoMonitorStatus.running ? '运行中' : '已停止' }}</span>
        </el-form-item>
        <el-form-item label="执行间隔(小时)">
          <el-input-number v-model="autoMonitorInterval" :min="1" :max="24" size="small" :disabled="autoMonitorStatus.running" />
        </el-form-item>
        <el-form-item label="上次执行时间">
          <span>{{ autoMonitorStatus.last_run_at || '尚未执行' }}</span>
        </el-form-item>
        <el-form-item label="上次提交任务数">
          <span>{{ autoMonitorStatus.last_task_count ?? 0 }}</span>
        </el-form-item>
        <el-form-item label="登录态检查">
          <span>{{ formatLoginStatus(autoMonitorStatus.last_login_status) }}</span>
        </el-form-item>
        <el-form-item v-if="autoMonitorStatus.error" label="上次错误">
          <el-tag type="danger" size="small">{{ autoMonitorStatus.error }}</el-tag>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 证据清理 -->
    <el-card class="settings-card">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">证据文件管理</span>
          <el-button size="small" @click="loadArtifactRetention" :loading="loadingRetention">扫描</el-button>
        </div>
      </template>
      <el-alert
        title="只清理数据库未引用的过期截图/HTML/摘要/素材；仍被监测、发布、报告引用的证据不会删除。"
        type="info"
        :closable="false"
        show-icon
        class="section-alert"
      />
      <el-form label-width="160px">
        <el-form-item label="保留天数">
          <el-input-number v-model="retentionDays" :min="1" :max="365" size="small" @change="loadArtifactRetention" />
          <span class="form-hint">默认保留 30 天内的所有证据</span>
        </el-form-item>
        <el-form-item label="证据总量">
          <span>{{ retention.total_files || 0 }} 个文件 / {{ formatBytes(retention.total_bytes) }}</span>
        </el-form-item>
        <el-form-item label="数据库引用">
          <span>{{ retention.referenced_existing_files || 0 }} 个现存文件受保护</span>
        </el-form-item>
        <el-form-item label="可清理">
          <el-tag :type="retention.candidate_files ? 'warning' : 'success'">
            {{ retention.candidate_files || 0 }} 个文件 / {{ formatBytes(retention.candidate_bytes) }}
          </el-tag>
        </el-form-item>
        <el-form-item label="安全规则">
          <span>{{ retention.safety_rule || '只清理未引用的过期证据' }}</span>
        </el-form-item>
        <el-form-item label="操作">
          <el-button size="small" @click="loadArtifactRetention" :loading="loadingRetention">预览清理</el-button>
          <el-button
            size="small"
            type="danger"
            :disabled="!retention.candidate_files"
            :loading="cleaningRetention"
            @click="cleanupArtifactRetention"
          >
            清理未引用证据
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 测试数据重置（开发记录 #9） -->
    <el-card class="settings-card">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">测试数据重置（监测历史/日志）</span>
          <el-button size="small" @click="loadHistoryReset" :loading="loadingHistoryReset">预览重置</el-button>
        </div>
      </template>
      <el-alert
        title="清空监测历史、任务队列、报告与发布工作流测试记录，并清理对应证据文件与日志；品牌/关键词/内容资产/发布配置全部保留。执行前会自动备份当前数据库。"
        type="warning"
        :closable="false"
        show-icon
        class="section-alert"
      />
      <el-form label-width="160px">
        <el-form-item label="历史记录">
          <span v-if="historyResetRows">共 {{ historyResetRows }} 行将被清空</span>
          <span v-else class="form-hint">点击「预览重置」查看将清理的内容</span>
        </el-form-item>
        <el-form-item label="证据/截图文件">
          <span>{{ historyReset.artifact_files || 0 }} 个 / {{ formatBytes(historyReset.artifact_bytes || 0) }}（发布证据保留）</span>
        </el-form-item>
        <el-form-item label="旧库快照">
          <span>{{ (historyReset.stale_db_backups || []).length }} 个 / {{ formatBytes(historyReset.stale_db_backup_bytes || 0) }}</span>
        </el-form-item>
        <el-form-item label="保留规则">
          <span>{{ historyReset.keep_rule || '品牌/关键词/内容/发布配置保留' }}</span>
        </el-form-item>
        <el-form-item label="操作">
          <el-button size="small" type="danger" :loading="executingHistoryReset" @click="executeHistoryReset">
            确认重置（先备份）
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import authFetch from '../utils/authFetch'
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { fallbackModeLabel, sessionStatusLabel } from '../utils/labels'

const llmProviders = ref([])
const llmUsage = ref({})
const loadingLlm = ref(false)
const testingLlm = ref('')
const captchaStatus = ref({ paid_available: false, provider: 'none', supported_types: [], fallback_mode: 'manual', notes: '...' })
const engineStatus = ref([])
const backendStatus = ref('checking')
const workerStatus = ref('checking')
const dbSize = ref('...')
const testingPlatform = ref('')
const loadingStatus = ref(false)
const retentionDays = ref(30)
const retention = ref({})
const loadingRetention = ref(false)
const cleaningRetention = ref(false)
// 测试数据重置
const historyReset = ref({})
const loadingHistoryReset = ref(false)
const executingHistoryReset = ref(false)
const historyResetRows = computed(() => {
  const rows = historyReset.value.history_table_rows || {}
  return Object.values(rows).reduce((sum, n) => sum + (Number(n) || 0), 0)
})
// 自动监测循环
const autoMonitorOn = ref(false)
const autoMonitorLoading = ref(false)
const autoMonitorInterval = ref(6)
const autoMonitorStatus = ref({ running: false, last_run_at: null, last_task_count: 0, last_login_status: {}, error: null })
const errorMessage = (error, fallback = '请求失败') => {
  return error?.detail || error?.message || fallback
}
const sessionLabel = sessionStatusLabel
const sessionTagType = (status) => ({
  logged_in: 'success',
  api_ready: 'warning',
  browser_open: 'warning',
  captcha_required: 'danger',
  expired: 'danger',
  not_connected: 'info',
  unknown: 'info'
}[status] || 'info')
const serviceLabel = (status) => ({
  running: '运行中',
  offline: '离线',
  checking: '检测中',
  unknown: '未知',
}[status] || '未知')
const serviceTagType = (status) => ({
  running: 'success',
  offline: 'danger',
  checking: 'warning',
  unknown: 'info',
}[status] || 'info')
const llmStatusLabel = (status) => ({
  success: '连接成功',
  failed: '连接失败',
  missing_key: '缺少密钥',
  disabled: '未启用',
  not_tested: '未测试',
}[status] || '未测试')
const llmStatusTag = (status) => ({
  success: 'success',
  failed: 'danger',
  missing_key: 'warning',
  disabled: 'info',
  not_tested: 'info',
}[status] || 'info')
const formatCost = (value) => `¥${Number(value || 0).toFixed(4)}`
const formatBytes = (value) => {
  const bytes = Number(value || 0)
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(2)} KB`
  return `${bytes} B`
}

const loadLlm = async () => {
  loadingLlm.value = true
  try {
    const providers = await authFetch('/api/llm/providers').then(async r => {
      const data = await r.json()
      if (!r.ok) throw data
      return data
    })
    llmProviders.value = (providers || []).map(item => ({ ...item, enabled: !!item.enabled, api_key_input: '' }))
    llmUsage.value = await authFetch('/api/llm/usage').then(async r => {
      const data = await r.json()
      if (!r.ok) throw data
      return data
    })
  } catch (e) {
    ElMessage.error(`大模型配置加载失败: ${errorMessage(e)}`)
  } finally {
    loadingLlm.value = false
  }
}

const saveLlmProvider = async (row) => {
  try {
    const payload = {
      base_url: row.base_url,
      default_model: row.default_model,
      enabled: !!row.enabled,
      prompt_price_per_1k: row.prompt_price_per_1k || 0,
      completion_price_per_1k: row.completion_price_per_1k || 0,
    }
    if (row.api_key_input) payload.api_key = row.api_key_input
    const res = await authFetch(`/api/llm/providers/${row.provider}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const data = await res.json()
    if (!res.ok) throw data
    Object.assign(row, { ...data, enabled: !!data.enabled, api_key_input: '' })
    ElMessage.success(`${row.display_name} 已保存`)
  } catch (e) {
    ElMessage.error(`保存失败: ${errorMessage(e)}`)
  }
}

const testLlm = async (row) => {
  testingLlm.value = row.provider
  try {
    const res = await authFetch(`/api/llm/providers/${row.provider}/test`, { method: 'POST' })
    const data = await res.json()
    if (!res.ok) throw data
    if (data.success) {
      ElMessage.success(`${row.display_name} 连接成功`)
    } else {
      ElMessage.warning(`${row.display_name} 测试未通过: ${data.message}`)
    }
    await loadLlm()
  } catch (e) {
    ElMessage.error(`测试失败: ${errorMessage(e)}`)
  } finally {
    testingLlm.value = ''
  }
}

const refreshAll = async () => {
  loadingStatus.value = true
  try {
    await loadLlm()
    // Captcha
    const cap = await authFetch('/api/captcha/status').then(async r => {
      const data = await r.json()
      if (!r.ok) throw data
      return data
    })
    captchaStatus.value = cap

    // Engine/session status
    const login = await authFetch('/api/platform-sessions').then(async r => {
      const data = await r.json()
      if (!r.ok) throw data
      return data
    })
    engineStatus.value = Object.entries(login || {}).map(([k, v]) => ({
      platform: k,
      name: ({ doubao: '豆包', yuanbao: '元宝', qianwen: '千问', web: '网页搜索' })[k] || k,
      mode: k === 'qianwen' && v.notes?.includes('DashScope') ? '官方接口' : '固定浏览器',
      status: v.status || 'unknown',
      port: v.port || '',
      profile_dir: v.profile_dir || '',
      notes: v.notes || '',
    }))
    // 加Web引擎
    engineStatus.value.unshift({ platform: 'web', name: '网页搜索', mode: '网页请求', status: 'logged_in', port: '-', profile_dir: '-', notes: '百度+必应，不代表AI平台引用' })

    // Backend/worker alive
    const sys = await authFetch('/api/system/status').then(async r => {
      const data = await r.json()
      if (!r.ok) throw data
      return data
    })
    backendStatus.value = sys?.backend?.status || 'unknown'
    workerStatus.value = sys?.worker?.status || 'unknown'

    // DB size
    const brandResp = await authFetch('/api/brands').then(async r => {
      const data = await r.json()
      if (!r.ok) throw data
      return data
    })
    const stats = await authFetch('/api/dashboard/all-stats').then(async r => {
      const data = await r.json()
      if (!r.ok) throw data
      return data
    })
    dbSize.value = `${Array.isArray(brandResp) ? brandResp.length : 0 || 0} 个品牌, ${stats?.totalKeywords || 0} 个关键词, ${stats?.verifiedCitationCount || 0} 条已验证引用`
    await loadArtifactRetention(false)
    await loadAutoMonitorStatus()
    ElMessage.success('状态已刷新')
  } catch (e) {
    ElMessage.error(`部分状态获取失败: ${errorMessage(e)}`)
  } finally {
    loadingStatus.value = false
  }
}

const loadArtifactRetention = async (showMessage = true) => {
  loadingRetention.value = true
  try {
    const res = await authFetch(`/api/system/artifact-retention?days=${retentionDays.value}`)
    const data = await res.json()
    if (!res.ok) throw data
    retention.value = data
    if (showMessage) ElMessage.success(`证据扫描完成：可清理 ${data.candidate_files || 0} 个文件`)
  } catch (e) {
    ElMessage.error(`证据扫描失败: ${errorMessage(e)}`)
  } finally {
    loadingRetention.value = false
  }
}

const cleanupArtifactRetention = async () => {
  try {
    await ElMessageBox.confirm(
      `将清理 ${retention.value.candidate_files || 0} 个未引用过期证据文件，数据库仍引用的证据会保留。确认执行？`,
      '确认清理证据',
      { confirmButtonText: '执行清理', cancelButtonText: '取消', type: 'warning' }
    )
    cleaningRetention.value = true
    const res = await authFetch(`/api/system/artifact-retention/cleanup?days=${retentionDays.value}`, { method: 'POST' })
    const data = await res.json()
    if (!res.ok) throw data
    retention.value = data
    ElMessage.success(`清理完成：删除 ${data.deleted_files || 0} 个文件，释放 ${formatBytes(data.deleted_bytes)}`)
    await loadArtifactRetention(false)
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(`清理失败: ${errorMessage(e)}`)
  } finally {
    cleaningRetention.value = false
  }
}

const loadHistoryReset = async (showMessage = true) => {
  loadingHistoryReset.value = true
  try {
    const res = await authFetch('/api/system/history-reset/preview')
    const data = await res.json()
    if (!res.ok) throw data
    historyReset.value = data
    if (showMessage) ElMessage.success('重置预览完成，请核对清理范围')
  } catch (e) {
    ElMessage.error(`重置预览失败: ${errorMessage(e)}`)
  } finally {
    loadingHistoryReset.value = false
  }
}

const executeHistoryReset = async () => {
  try {
    await ElMessageBox.confirm(
      `将清空监测历史 ${historyResetRows.value} 行、删除 ${historyReset.value.artifact_files || 0} 个证据文件并截断日志。` +
      '执行前会自动备份当前数据库到 backend/db_archive/。品牌/关键词/内容资产/发布配置保留。确认执行？',
      '确认重置测试数据',
      { confirmButtonText: '确认重置', cancelButtonText: '取消', type: 'warning' }
    )
    await ElMessageBox.prompt('输入 RESET 以确认执行', '二次确认', { confirmButtonText: '执行', cancelButtonText: '取消' })
    executingHistoryReset.value = true
    const res = await authFetch('/api/system/history-reset/execute?confirm=RESET', { method: 'POST' })
    const data = await res.json()
    if (!res.ok) throw data
    ElMessage.success(`重置完成：备份至 ${data.backup_path || '（未备份）'}；数据库现为 ${formatBytes(data.database_bytes_after || 0)}`)
    await Promise.all([loadHistoryReset(false), refreshAll()])
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(`重置失败: ${errorMessage(e)}`)
  } finally {
    executingHistoryReset.value = false
  }
}

const runScheduler = async (mode) => {
  const label = mode === 'daily' ? '每日增量扫描' : '全量扫描'
  try {
    await ElMessageBox.confirm(
      `${label}会为所有启用品牌提交监测任务。AI平台未登录时会记录失败证据，不会伪装成功。`,
      `确认运行${label}`,
      { confirmButtonText: '提交任务', cancelButtonText: '取消', type: mode === 'weekly' ? 'warning' : 'info' }
    )
    ElMessage.info(`正在启动${label}...`)
    const res = await authFetch(`/api/system/run-scheduler?mode=${mode}`, { method: 'POST' })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || data?.error || '调度接口失败')
    const count = data?.task_count ?? data?.task_ids?.length ?? 0
    ElMessage.success(`${label}已提交，任务数: ${count}`)
    await refreshAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(`${label}启动失败: ${errorMessage(e)}`)
  }
}

const openLogin = async (platform) => {
  try {
    const res = await authFetch(`/api/platform-sessions/${platform}/open-login`, { method: 'POST' })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || data?.notes || '打开失败')
    ElMessage.success(data?.notes || '已打开固定登录浏览器')
    await refreshAll()
  } catch (e) {
    ElMessage.error(e?.message || '打开登录窗口失败')
  }
}

const selfTest = async (platform) => {
  testingPlatform.value = platform
  try {
    ElMessage.info('正在检测页面登录态（不发送消息、不消耗提问额度）...')
    const res = await authFetch(`/api/platform-sessions/${platform}/self-test`, { method: 'POST' })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || '自检失败')
    if (data.success) {
      ElMessage.success(`${platformName(platform)} 登录态可用（无侵入检测，未发送消息）`)
    } else if (data.captcha_status === 'detected') {
      ElMessage.warning(`${platformName(platform)} 需要验证码处理：${data.failure_reason || ''}`)
    } else {
      ElMessage.error(`${platformName(platform)} 自检失败：${data.failure_reason || '登录态未通过'}`)
    }
    await refreshAll()
  } catch (e) {
    ElMessage.error(e?.message || '运行自检失败')
  } finally {
    testingPlatform.value = ''
  }
}

const platformName = (platform) => ({
  doubao: '豆包',
  qianwen: '千问',
  yuanbao: '元宝',
})[platform] || platform

// ---- 自动监测循环 ----
const loadAutoMonitorStatus = async () => {
  try {
    const res = await authFetch('/api/system/auto-monitor')
    const data = await res.json()
    if (!res.ok) throw data
    autoMonitorStatus.value = data
    autoMonitorOn.value = !!data.running
    if (data.interval_hours) autoMonitorInterval.value = data.interval_hours
  } catch (e) {
    // 静默失败，不打扰用户
  }
}

const toggleAutoMonitor = async (val) => {
  autoMonitorLoading.value = true
  try {
    const action = val ? 'start' : 'stop'
    const url = `/api/system/auto-monitor?action=${action}&interval=${autoMonitorInterval.value}`
    const res = await authFetch(url, { method: 'POST' })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || '操作失败')
    ElMessage.success(data.message || (val ? '自动监测已启动' : '已停止'))
    await loadAutoMonitorStatus()
  } catch (e) {
    ElMessage.error(e?.message || '自动监测操作失败')
    autoMonitorOn.value = !val  // 回滚开关
  } finally {
    autoMonitorLoading.value = false
  }
}

const formatLoginStatus = (status) => {
  if (!status || typeof status !== 'object') return '—'
  const parts = Object.entries(status).map(([k, v]) => {
    const name = { doubao: '豆包', qianwen: '千问', yuanbao: '元宝' }[k] || k
    return `${name}:${v ? '正常' : '未登录'}`
  })
  return parts.join('，') || '—'
}

onMounted(refreshAll)
</script>

<style scoped>
.settings { padding: 20px; max-width: 1040px; margin: 0 auto; }
.settings-card { margin-bottom: 20px; }
.card-title { font-weight: bold; }
.card-header-row { display: flex; align-items: center; justify-content: space-between; }
.section-alert { margin-bottom: 16px; }
.form-hint { margin-left: 10px; font-size: 12px; color: #909399; }
.price-tags { display: flex; gap: 8px; flex-wrap: wrap; }
.price-inputs { display: grid; grid-template-columns: 1fr; gap: 6px; }
.usage-grid { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 12px; margin-top: 16px; }
.usage-item { border: 1px solid #ebeef5; border-radius: 6px; padding: 12px; background: #fafafa; display: flex; flex-direction: column; gap: 6px; }
.usage-item span { color: #606266; font-size: 12px; }
.usage-item b { font-size: 20px; color: #303133; }
.muted { color: #c0c4cc; font-size: 12px; }
.action-buttons { display: flex; flex-direction: column; align-items: stretch; gap: 8px; }
.action-buttons :deep(.el-button) { margin-left: 0; width: 100%; }
</style>
