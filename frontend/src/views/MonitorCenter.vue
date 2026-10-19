<template>
  <div class="monitor-center">
    <el-card class="step-card">
      <div class="step-header">
        <el-steps :active="currentStep" finish-status="success" align-center>
          <el-step title="选择品牌" />
          <el-step title="选择关键词" />
          <el-step title="选择平台" />
          <el-step title="执行监测" />
        </el-steps>
      </div>

      <div class="step-content">
        <!-- Step 1: Brand -->
        <div v-show="currentStep === 0" class="step-panel">
          <h3>选择品牌</h3>
          <el-select v-model="selectedBrandId" placeholder="请选择品牌" style="width: 300px;" @change="handleBrandChange" clearable>
            <el-option v-for="brand in brands" :key="brand.id" :label="brand.name" :value="brand.id" />
          </el-select>
          <div class="step-actions">
            <el-button type="primary" :disabled="!selectedBrandId" @click="currentStep = 1">下一步</el-button>
          </div>
        </div>

        <!-- Step 2: Keywords -->
        <div v-show="currentStep === 1" class="step-panel">
          <h3>选择关键词 <span class="sub-hint">已选 {{ selectedKeywords.length }} 个</span></h3>
          <div class="keyword-selection">
            <div class="selection-header">
              <el-checkbox v-model="selectAll" :indeterminate="isIndeterminate" @change="handleSelectAll">全选</el-checkbox>
              <span class="selection-count">共 {{ brandKeywords.length }} 个关键词</span>
            </div>
            <div v-if="brandKeywords.length === 0" class="empty-tip">该品牌下暂无关键词</div>
            <el-checkbox-group v-model="selectedKeywords" v-else>
              <el-checkbox v-for="kw in brandKeywords" :key="kw.id" :value="kw.id">
                {{ kw.word }}
              </el-checkbox>
            </el-checkbox-group>
          </div>
          <div class="step-actions">
            <el-button @click="currentStep = 0">上一步</el-button>
            <el-button type="primary" :disabled="selectedKeywords.length === 0" @click="currentStep = 2">下一步</el-button>
          </div>
        </div>

        <!-- Step 3: Platform -->
        <div v-show="currentStep === 2" class="step-panel">
          <h3>选择监测平台</h3>
          <div class="platform-grid">
            <div v-for="plat in platformList" :key="plat.id"
              class="platform-item" :class="{ active: selectedPlatform === plat.id }"
              @click="selectedPlatform = plat.id">
              <div class="platform-icon">{{ plat.icon }}</div>
              <div class="platform-info">
                <div class="platform-name">{{ plat.name }}</div>
                <div class="platform-desc">{{ plat.desc }}</div>
              </div>
              <div v-if="selectedPlatform === plat.id" class="platform-check">✓</div>
            </div>
          </div>
          <div class="monitor-tip">
            <el-icon><InfoFilled /></el-icon>
            <span>异步模式：提交后立即返回任务编号，可在下方任务列表中查看进度</span>
          </div>
          <div style="margin-top:12px; display:flex; align-items:center; gap:12px;">
            <el-switch v-model="autoRoundMode" active-text="自动轮巡" inactive-text="人工单次" />
            <el-input-number v-model="autoRounds" :min="1" :max="20" :disabled="!autoRoundMode" size="small" />
            <span class="sub-hint">轮巡次数</span>
          </div>
          <div v-if="autoRoundMode" style="margin-top:12px; display:flex; flex-wrap:wrap; align-items:center; gap:12px;">
            <el-date-picker v-model="scheduledStartAt" type="datetime" placeholder="请选择开始时间" format="YYYY年MM月DD日 HH时mm分ss秒" value-format="YYYY-MM-DD HH:mm:ss" style="width:260px;" />
            <el-input-number v-model="intervalMinutes" :min="1" :max="240" size="small" />
            <span class="sub-hint">轮巡间隔：分钟</span>
            <el-input-number v-model="maxRounds" :min="1" :max="100" size="small" />
            <span class="sub-hint">最大轮次：次</span>
          </div>
          <div class="step-actions">
            <el-button @click="currentStep = 1">上一步</el-button>
            <el-button type="primary" @click="handleStartMonitor" :loading="submitting">开始监测</el-button>
          </div>
        </div>

        <!-- Step 4: Submit result -->
        <div v-show="currentStep === 3" class="step-panel">
          <div v-if="currentTask" class="task-submitted">
            <el-result icon="success" title="监测任务已创建"
              :sub-title="`任务编号：${currentTask.task_id} | 共 ${currentTask.total_operations} 次操作`">
              <template #extra>
                <p>品牌：{{ currentBrandName }} | 关键词：{{ selectedKeywords.length }}个 | 平台：{{ platformLabel(selectedPlatform) }}</p>
                <el-button type="primary" style="margin-top:16px;" @click="openTasksPanel">查看任务进度</el-button>
                <el-button style="margin-left:12px;" @click="viewLatestResults">查看结果详情</el-button>
                <el-button style="margin-left:12px;" @click="resetMonitor">重新监测</el-button>
              </template>
            </el-result>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 任务列表面板 -->
    <el-card class="tasks-card">
      <template #header>
        <div class="card-header-row">
          <span>监测任务列表</span>
          <div class="header-actions">
            <el-tag v-if="autoRefreshing" type="success" size="small" class="auto-refresh-tag">自动刷新中</el-tag>
            <el-button size="small" @click="toggleAutoRefresh">
              {{ autoRefreshing ? '停止刷新' : '自动刷新' }}
            </el-button>
            <el-button size="small" @click="fetchTasks">刷新</el-button>
          </div>
        </div>
      </template>

      <el-table :data="taskList" v-loading="taskLoading" stripe max-height="400">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="业务判定" width="110">
          <template #default="{ row }">
            <el-tag :type="businessStatusTagType(row.business_status)" size="small">
              {{ businessStatusLabel(row.business_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="140">
          <template #default="{ row }">
            <el-progress :percentage="row.progress_pct || 0"
              :status="row.status === 'completed' ? 'success' : row.status === 'failed' ? 'exception' : ''"
              :stroke-width="16" :text-inside="true" />
          </template>
        </el-table-column>
        <el-table-column prop="progress_msg" label="消息" min-width="220" show-overflow-tooltip />
        <el-table-column prop="created_at" label="创建时间" width="170" />
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button size="small" @click="viewTaskResult(row)" :disabled="!['completed', 'failed'].includes(row.status)">详情</el-button>
              <el-button size="small" type="warning" @click="cancelTask(row)" :disabled="!['pending', 'running'].includes(row.status)">取消</el-button>
              <el-button size="small" type="danger" @click="deleteTask(row)" :disabled="row.status === 'running'">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 结果详情弹窗 -->
    <el-dialog v-model="resultDialogVisible" title="监测结果详情" width="90%" top="30px">
      <el-alert v-if="taskResultSummary"
        :title="`${businessStatusLabel(taskResultSummary.businessStatus)}：引用率 ${taskResultSummary.cited}/${taskResultSummary.total} (${taskResultSummary.rate}%)`"
        :description="taskResultSummary.businessReason || '有效业务结果必须包含真实品牌露出和有效可验证来源。'"
        :type="taskResultSummary.businessStatus === 'passed' ? 'success' : taskResultSummary.businessStatus === 'failed_quality_gate' ? 'error' : taskResultSummary.businessStatus === 'brand_mentioned_unverified' ? 'warning' : 'info'"
        show-icon :closable="false" style="margin-bottom:16px;" />
      <el-alert v-if="autoContentInfo" type="warning" show-icon :closable="false" style="margin-bottom:16px;">
        <template #title>
          未引用关键词自动补内容：{{ autoContentInfo.gap_count }} 个关键词已转入自动生成任务 #{{ autoContentInfo.task_id }}
        </template>
        <div style="font-size:12px;line-height:1.8;">
          关键词：{{ autoContentInfo.gaps.map(g => g.keyword).join('、') }}<br />
          生成完成后为草稿状态，可在「运营中心 → 内容资产」审核后进入发布链路。
        </div>
      </el-alert>
      <el-table :data="resultDetails" v-loading="resultLoading" stripe max-height="500">
        <el-table-column prop="keyword_word" label="关键词" min-width="130" />
        <el-table-column label="平台" width="100">
          <template #default="{ row }">{{ platformLabel(row.platform) }}</template>
        </el-table-column>
        <el-table-column label="引用" width="60">
          <template #default="{ row }">
            <el-tag :type="row.is_cited ? 'success' : 'info'" size="small">{{ row.is_cited ? '是' : '否' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="验证状态" width="100">
          <template #default="{ row }">
            <el-tag :type="verificationTagType(row.verification_status)" size="small">
              {{ verificationLabel(row.verification_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="可验证来源" min-width="220">
          <template #default="{ row }">
            <div v-if="sourceLinks(row).length" class="source-list">
              <a v-for="src in sourceLinks(row).slice(0, 3)" :key="src.url" :href="src.url" target="_blank" rel="noopener noreferrer">
                {{ src.domain || src.title || src.url }}
              </a>
            </div>
            <span v-else class="muted">无可验证来源</span>
          </template>
        </el-table-column>
        <el-table-column label="截图" width="90">
          <template #default="{ row }">
            <a v-if="evidenceLink(row, 'screenshot')" :href="evidenceLink(row, 'screenshot')" target="_blank" rel="noopener noreferrer">查看</a>
            <el-tooltip v-else-if="row.screenshot_path" content="截图文件已缺失或不可访问" placement="top">
              <span class="muted">已缺失</span>
            </el-tooltip>
            <span v-else class="muted">无</span>
          </template>
        </el-table-column>
        <el-table-column label="原始证据" width="90">
          <template #default="{ row }">
            <a v-if="evidenceLink(row, 'raw_artifact')" :href="evidenceLink(row, 'raw_artifact')" target="_blank" rel="noopener noreferrer">查看</a>
            <el-tooltip v-else-if="row.raw_artifact_path" content="原始证据文件已缺失或不可访问" placement="top">
              <span class="muted">已缺失</span>
            </el-tooltip>
            <span v-else class="muted">无</span>
          </template>
        </el-table-column>
        <el-table-column label="回答摘要" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            {{ (row.answer_text || row.snippet || row.cited_sources || '').slice(0, 180) || '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="failure_reason" label="失败/待处理原因" min-width="180" show-overflow-tooltip />
        <el-table-column prop="total_results" label="搜索结果数" width="90" />
        <el-table-column prop="notes" label="备注" min-width="180" show-overflow-tooltip />
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup>
import authFetch from '../utils/authFetch'
import { evidenceUrl } from '../utils/evidenceUrl'
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { InfoFilled } from '@element-plus/icons-vue'
import { getBrands } from '../api/brands'
import { getKeywordsByBrand } from '../api/keywords'
import { runMonitor, getTaskStatus, getMonitorResults } from '../api/monitor'
import { listTasks } from '../api/tasks'
import { platformLabel } from '../utils/labels'

const route = useRoute()

const platformList = [
  { id: 'ai', name: 'AI三平台', icon: '智', desc: '豆包+千问+元宝真实检测' },
  { id: 'web', name: '网页搜索', icon: '网', desc: '百度+必应搜索引擎收录' },
  { id: 'doubao', name: '豆包', icon: '豆', desc: '豆包AI平台' },
  { id: 'qianwen', name: '千问', icon: '问', desc: '千问AI平台' },
  { id: 'yuanbao', name: '元宝', icon: '元', desc: '元宝AI平台' },
  { id: 'all', name: '全平台', icon: '全', desc: '网页+AI全部平台一次性执行' },
]

const currentStep = ref(0)
const brands = ref([])
const selectedBrandId = ref(null)
const brandKeywords = ref([])
const selectedKeywords = ref([])
const selectedPlatform = ref('ai')
const submitting = ref(false)
const autoRoundMode = ref(false)
const autoRounds = ref(1)
const scheduledStartAt = ref('')
const intervalMinutes = ref(30)
const maxRounds = ref(10)
const selectAll = ref(false)
const currentBrandName = ref('')

// 任务相关
const currentTask = ref(null)
const taskList = ref([])
const taskLoading = ref(false)
const taskResultSummary = ref(null)
const autoContentInfo = ref(null)
const resultDialogVisible = ref(false)
const resultDetails = ref([])
const resultLoading = ref(false)

// 自动刷新
const autoRefreshing = ref(false)
let refreshTimer = null

const isIndeterminate = computed(() => {
  const kws = brandKeywords.value
  return selectedKeywords.value.length > 0 && selectedKeywords.value.length < kws.length
})

const statusTagType = (s) => ({ pending:'info', running:'warning', completed:'success', failed:'danger', cancelled:'info' }[s] || 'info')
const statusLabel = (s) => ({ pending:'等待中', running:'运行中', completed:'已完成', failed:'失败', cancelled:'已取消' }[s] || s || '未知')
const businessStatusLabel = (s) => ({ passed:'业务通过', failed_quality_gate:'品牌未露出', brand_mentioned_unverified:'品牌已露出(来源未验证)', completed_with_errors:'部分完成', pending:'待判定' }[s] || s || '未知')
const businessStatusTagType = (s) => ({ passed:'success', failed_quality_gate:'danger', brand_mentioned_unverified:'warning', completed_with_errors:'warning', pending:'info' }[s] || 'info')
const verificationLabel = (s) => ({ verified_citation:'已验证', unverified_citation:'未验证', not_cited:'未引用', failed:'失败' }[s] || s || '未知')
const verificationTagType = (s) => ({ verified_citation:'success', unverified_citation:'warning', not_cited:'info', failed:'danger' }[s] || 'info')
const sourceLinks = (row) => {
  if (Array.isArray(row.sources) && row.sources.length) return row.sources.filter(s => s.url && s.source_verified)
  if (row.source_verified && row.source_url) return [{ url: row.source_url, domain: row.source_domain, title: row.cited_sources }]
  return []
}
// 证据链接：优先用后端计算好的 screenshot_url / raw_artifact_url（兼容
// Windows 反斜杠绝对路径与中文文件名）；老数据没有该字段时做兜底转换。
const evidenceLink = (row, kind) => {
  if (kind === 'screenshot') return evidenceUrl(row.screenshot_path, row.screenshot_url)
  return evidenceUrl(row.raw_artifact_path, row.raw_artifact_url)
}

const handleBrandChange = async (val) => {
  if (!val) return
  const brand = brands.value.find(b => b.id === val)
  currentBrandName.value = brand?.name || ''
  selectedKeywords.value = []
  selectAll.value = false
  try {
    brandKeywords.value = (await getKeywordsByBrand(val)) || []
  } catch { brandKeywords.value = [] }
}

const applyRouteSelection = async () => {
  const brandId = Number(route.query.brand_id)
  if (!brandId) return
  selectedBrandId.value = brandId
  await handleBrandChange(brandId)

  const ids = String(route.query.keyword_ids || '')
    .split(',')
    .map(id => Number(id.trim()))
    .filter(Boolean)
  if (ids.length) {
    const availableIds = new Set(brandKeywords.value.map(item => item.id))
    selectedKeywords.value = ids.filter(id => availableIds.has(id))
    if (selectedKeywords.value.length) {
      currentStep.value = 2
      selectAll.value = selectedKeywords.value.length === brandKeywords.value.length
    }
  } else {
    currentStep.value = 1
  }

  const routePlatform = String(route.query.platform || '')
  if (routePlatform && platformList.some(item => item.id === routePlatform)) {
    selectedPlatform.value = routePlatform
  }
}

const handleSelectAll = (val) => {
  selectedKeywords.value = val ? brandKeywords.value.map(k => k.id) : []
}

const handleStartMonitor = async () => {
  if (!selectedBrandId.value || selectedKeywords.value.length === 0) {
    ElMessage.warning('请选择品牌和关键词')
    return
  }
  if (autoRoundMode.value) {
    if (!scheduledStartAt.value) {
      ElMessage.warning('请设置定时开始时间')
      return
    }
    if (!intervalMinutes.value || intervalMinutes.value < 1) {
      ElMessage.warning('请设置有效的轮巡间隔')
      return
    }
    if (!maxRounds.value || maxRounds.value < 1) {
      ElMessage.warning('请设置有效的最大轮次')
      return
    }
  }
  submitting.value = true
  try {
    const data = await runMonitor({
      brand_id: selectedBrandId.value,
      keyword_ids: selectedKeywords.value,
      platform: selectedPlatform.value,
      run_mode: autoRoundMode.value ? 'auto' : 'manual',
      auto_rounds: autoRoundMode.value ? Math.min(autoRounds.value, maxRounds.value) : 1,
      scheduled_start_at: autoRoundMode.value ? scheduledStartAt.value : null,
      interval_minutes: autoRoundMode.value ? intervalMinutes.value : null,
    })
    currentTask.value = data
    currentStep.value = 3
    ElMessage.success(`任务已创建 (ID: ${data.task_id})`)
    // 3秒后自动打开任务列表
    setTimeout(() => { fetchTasks(); openTasksPanel() }, 2000)
  } catch (error) {
    ElMessage.error('提交失败: ' + (error?.response?.data?.message || error?.message || '未知错误'))
  } finally { submitting.value = false }
}

const openTasksPanel = async () => {
  await fetchTasks()
  startAutoRefresh()
}

const fetchTasks = async () => {
  taskLoading.value = true
  try {
    taskList.value = (await listTasks({ task_type: 'monitor' })) || []
    // 如果有正在运行的任务且自动刷新没开，自动开启
    const hasRunning = taskList.value.some(t => t.status === 'running' || t.status === 'pending')
    if (hasRunning && !autoRefreshing.value) {
      startAutoRefresh()
    }
  } catch (error) {
    ElMessage.error(`获取任务列表失败: ${error?.response?.data?.detail || error?.message || '未知错误'}`)
  }
  finally { taskLoading.value = false }
}

const cancelTask = async (task) => {
  try {
    await ElMessageBox.confirm(`取消监测任务 #${task.id}？已产生的监测结果和证据不会删除。`, '取消任务', {
      confirmButtonText: '取消任务',
      cancelButtonText: '返回',
      type: 'warning',
    })
    const res = await authFetch(`/api/tasks/${task.id}/cancel`, { method: 'POST' })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || data?.message || '取消失败')
    ElMessage.success(`任务 #${task.id} 已取消`)
    await fetchTasks()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.message || '取消任务失败')
  }
}

const deleteTask = async (task) => {
  try {
    await ElMessageBox.confirm(`删除监测任务 #${task.id} 的列表记录？历史结果和证据会保留。`, '删除任务', {
      confirmButtonText: '删除',
      cancelButtonText: '返回',
      type: 'warning',
    })
    const res = await authFetch(`/api/monitor/tasks/${task.id}`, { method: 'DELETE' })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || data?.message || '删除失败')
    ElMessage.success(`任务 #${task.id} 已删除`)
    await fetchTasks()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.message || '删除任务失败')
  }
}

const startAutoRefresh = () => {
  autoRefreshing.value = true
  if (refreshTimer) clearInterval(refreshTimer)
  refreshTimer = setInterval(async () => {
    await fetchTasks()
    // 如果没有running/pending的任务了，停止自动刷新
    const hasRunning = taskList.value.some(t => t.status === 'running' || t.status === 'pending')
    if (!hasRunning) stopAutoRefresh()
  }, 3000)
}

const stopAutoRefresh = () => {
  autoRefreshing.value = false
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null }
}

const toggleAutoRefresh = () => {
  if (autoRefreshing.value) stopAutoRefresh()
  else startAutoRefresh()
}

const viewTaskResult = async (task) => {
  if (!['completed', 'failed'].includes(task.status)) return
  resultLoading.value = true
  resultDialogVisible.value = true
  autoContentInfo.value = null
  try {
    const data = await getTaskStatus(task.id)
    autoContentInfo.value = data?.result?.auto_content || null
    if (data?.answers?.length) {
      resultDetails.value = data.answers
      const verified = data.answers.filter(r => r.verification_status === 'verified_citation').length
      taskResultSummary.value = {
        total: data.answers.length,
        cited: verified,
        rate: data.answers.length ? ((verified / data.answers.length) * 100).toFixed(1) : '0',
        businessStatus: data.business_status || data.run?.business_status || 'pending',
        businessReason: data.business_failure_reason || data.run?.business_failure_reason || '',
      }
    } else if (data?.result?.results?.length) {
      const result = data.result
      resultDetails.value = result.results
      const verified = result.verified_cited || result.results.filter(r => r.verification_status === 'verified_citation').length
      taskResultSummary.value = {
        total: result.total,
        cited: verified,
        rate: result.total ? ((verified / result.total) * 100).toFixed(1) : '0',
        businessStatus: result.business_status || data.business_status || 'pending',
        businessReason: result.business_failure_reason || data.business_failure_reason || '',
      }
    } else {
      const res = await getMonitorResults({ brand_id: selectedBrandId.value || undefined, limit: 200 })
      resultDetails.value = res || []
      const cited = res.filter(r => r.verification_status === 'verified_citation').length
      taskResultSummary.value = { total: res.length, cited, rate: res.length ? ((cited / res.length) * 100).toFixed(1) : '0', businessStatus: 'pending', businessReason: '' }
    }
  } catch (error) {
    ElMessage.error(`获取结果失败: ${error?.response?.data?.detail || error?.message || '未知错误'}`)
  }
  finally { resultLoading.value = false }
}

const viewLatestResults = async () => {
  resultLoading.value = true
  resultDialogVisible.value = true
  autoContentInfo.value = null
  try {
    const brandId = selectedBrandId.value
    const response = await authFetch(`/api/monitor/results?brand_id=${brandId}&limit=20`)
    const res = await response.json()
    if (!response.ok) throw new Error(res?.detail || res?.message || '结果接口失败')
    resultDetails.value = res || []
    const cited = res.filter(r => r.verification_status === 'verified_citation').length
    taskResultSummary.value = { total: res.length, cited, rate: res.length ? ((cited / res.length) * 100).toFixed(1) : '0', businessStatus: 'pending', businessReason: '' }
  } catch (error) {
    ElMessage.error(`获取结果失败: ${error?.message || '未知错误'}`)
  }
  finally { resultLoading.value = false }
}

const resetMonitor = () => {
  currentStep.value = 0; selectedBrandId.value = null; brandKeywords.value = []
  selectedKeywords.value = []; currentTask.value = null
}

onMounted(async () => {
  try {
    brands.value = await getBrands() || []
  } catch (error) {
    ElMessage.error(`品牌列表加载失败: ${error?.message || '未知错误'}`)
  }
  await applyRouteSelection()
  fetchTasks()
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
.monitor-center { padding: 20px; }
.step-card { margin-bottom: 24px; }
.tasks-card { min-height: 200px; }
.step-header { margin-bottom: 30px; padding: 0 20px; }
.step-content { padding: 20px; }
.step-panel { max-width: 720px; margin: 0 auto; }
.step-panel h3 { margin-bottom: 20px; color: #303133; }
.sub-hint { font-size: 13px; color: #909399; font-weight: normal; margin-left: 8px; }
.step-actions { margin-top: 24px; display: flex; justify-content: center; gap: 16px; }

.keyword-selection { border: 1px solid #dcdfe6; border-radius: 8px; padding: 16px; max-height: 320px; overflow-y: auto; }
.selection-header { display: flex; justify-content: space-between; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #ebeef5; }
.selection-count { color: #909399; font-size: 13px; }
.empty-tip { color: #909399; text-align: center; padding: 20px; }

.platform-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
.platform-item { display: flex; align-items: center; gap: 12px; padding: 14px; border: 2px solid #dcdfe6; border-radius: 8px; cursor: pointer; transition: all 0.2s; }
.platform-item:hover { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.platform-item.active { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); box-shadow: 0 2px 8px rgba(76, 111, 255, 0.15); }
.platform-icon { font-size: 24px; width: 40px; text-align: center; }
.platform-info { flex: 1; }
.platform-name { font-weight: 600; font-size: 14px; color: var(--el-text-color-primary); }
.platform-desc { font-size: 11px; color: var(--el-text-color-secondary); margin-top: 2px; }
.platform-check { font-size: 20px; color: var(--el-color-primary); font-weight: bold; }

.monitor-tip { display: flex; gap: 8px; padding: 12px; background: #fdf6ec; border-radius: 6px; font-size: 13px; color: #e6a23c; margin-bottom: 10px; }

.task-submitted { text-align: center; padding: 20px 0; }
.task-submitted p { color: #606266; margin-top: 8px; }

.card-header-row { display: flex; justify-content: space-between; align-items: center; font-weight: bold; }
.source-list { display: flex; flex-direction: column; gap: 3px; }
.source-list a { color: var(--el-color-primary); text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-list a:hover { text-decoration: underline; }
.muted { color: #c0c4cc; font-size: 12px; }

/* 行内操作列：统一间距与垂直对齐，避免按钮错位/换行不齐 */
.row-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.row-actions .el-button + .el-button { margin-left: 0; }
/* 卡片头操作区：统一间距，替代手工 margin */
.header-actions { display: flex; align-items: center; gap: 8px; }
.header-actions .auto-refresh-tag { margin-right: 0; }
</style>
