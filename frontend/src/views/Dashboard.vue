<template>
  <div class="dashboard">
    <div class="dashboard-header">
      <el-select v-model="selectedBrandId" placeholder="选择品牌" style="width: 220px;" @change="fetchAll" clearable>
        <el-option label="全部品牌" value="all" />
        <el-option v-for="brand in brands" :key="brand.id" :label="brand.name" :value="brand.id" />
      </el-select>
      <span class="header-hint">已选择: {{ selectedBrandId === 'all' || !selectedBrandId ? '全部品牌' : selectedBrandName }}</span>
      <el-button size="small" type="primary" @click="goMonitor" style="margin-left:8px;">运行监测</el-button>
      <el-button size="small" @click="fetchAll(true)" :loading="loadingData">刷新</el-button>
    </div>

    <div v-if="loadingData && !hasData" class="empty-state">
      <el-icon class="empty-icon is-loading" :size="72"><Loading /></el-icon>
      <div class="loading-text">正在读取真实监测数据...</div>
    </div>

    <div v-else-if="!hasData" class="empty-state">
      <el-empty description="暂无数据，请先进行监测">
        <template #image><el-icon class="empty-icon" :size="80"><DataAnalysis /></el-icon></template>
      </el-empty>
    </div>

    <template v-else>
      <!-- KPI 卡片 -->
      <div class="kpi-grid">
        <el-card class="kpi-card">
          <div class="kpi-value">{{ stats.totalKeywords || 0 }}</div>
          <div class="kpi-label">关键词总数</div>
        </el-card>
        <el-card class="kpi-card">
          <div class="kpi-value">{{ stats.contentAssetCount || stats.publishedContent || 0 }}</div>
          <div class="kpi-label">内容资产</div>
        </el-card>
        <el-card class="kpi-card">
          <div class="kpi-value kpi-blue">{{ stats.verifiedCitationCount || 0 }}</div>
          <div class="kpi-label">已验证引用</div>
        </el-card>
        <el-card class="kpi-card">
          <div class="kpi-value kpi-gold">{{ stats.citationRate || 0 }}%</div>
          <div class="kpi-label">已验证引用率</div>
        </el-card>
        <el-card class="kpi-card">
          <div class="kpi-value kpi-green">{{ stats.evidenceCompleteRate || 0 }}%</div>
          <div class="kpi-label">来源完整率</div>
        </el-card>
      </div>

      <div class="evidence-strip">
        <el-tag type="success">有来源链接 {{ stats.withSourceUrl || 0 }}</el-tag>
        <el-tag type="warning">未验证引用 {{ stats.unverifiedCitationCount || 0 }}</el-tag>
        <el-tag type="info">有截图 {{ stats.withScreenshot || 0 }}</el-tag>
        <el-tag type="primary">有回答原文 {{ stats.withAnswerText || 0 }}</el-tag>
        <el-tag type="success">已验证发布 {{ stats.verifiedPublishedCount || 0 }}</el-tag>
        <el-tag type="info">发布任务 {{ stats.publishingTaskCount || 0 }}</el-tag>
        <el-tag>总监测 {{ stats.totalMonitorCount || 0 }}</el-tag>
      </div>

      <div class="dashboard-grid">
        <!-- 趋势图 -->
        <el-card class="chart-card">
          <template #header>
            <div class="card-header-row">
              <span>{{ trendTitle }}</span>
	              <el-radio-group v-model="trendMode" size="small" @change="fetchAll(false)">
                <el-radio-button value="month">月</el-radio-button>
                <el-radio-button value="week">周</el-radio-button>
                <el-radio-button value="day">日</el-radio-button>
              </el-radio-group>
            </div>
          </template>
          <div v-if="trendData.length > 0" class="chart-area">
            <div class="bar-chart">
              <div v-for="(item, i) in trendData" :key="i" class="bar-group">
                <div class="bar" :style="{ height: barHeight(item.count) + 'px' }">
                  <span v-if="item.count > 0" class="bar-value">{{ item.count }}</span>
                </div>
                <div class="bar-label">{{ item.label?.slice(-5) }}</div>
              </div>
            </div>
          </div>
          <div v-else class="chart-empty">暂无趋势数据</div>
        </el-card>

        <!-- 最近监测 -->
        <el-card class="recent-card">
          <template #header><span>最近监测结果</span></template>
          <div v-if="recentResults.length > 0" class="recent-list">
            <div v-for="(r, i) in recentResults.slice(0, 10)" :key="i" class="recent-item">
              <div class="recent-keyword">{{ r.keyword }}</div>
              <div class="recent-meta">
                <el-tag :type="r.is_cited ? 'success' : 'info'" size="small">{{ r.is_cited ? '被引用' : '未引用' }}</el-tag>
                <el-tag :type="verificationTagType(r.verification_status)" size="small">{{ verificationLabel(r.verification_status) }}</el-tag>
                <el-tag size="small" type="warning">{{ platformLabel(r.platform) }}</el-tag>
                <span v-if="r.brand_name" class="recent-brand">{{ r.brand_name }}</span>
                <span class="recent-time">{{ r.monitored_at }}</span>
              </div>
              <div class="recent-source" v-if="r.source_url">
                <a :href="r.source_url" target="_blank" class="source-link" rel="noopener noreferrer">
                  {{ r.source_domain || r.source_url.slice(0, 50) }}
                </a>
                <el-tag size="small" type="success" v-if="isEvidenceComplete(r)" class="tag-cited">已验证来源</el-tag>
                <el-tag size="small" type="warning" v-else class="tag-cited">来源待验证</el-tag>
              </div>
              <div class="recent-source" v-if="r.screenshot_verified || r.raw_artifact_verified">
                <a v-if="artifactUrl(r.screenshot_path, r.screenshot_url)" :href="artifactUrl(r.screenshot_path, r.screenshot_url)" target="_blank" class="source-link" rel="noopener noreferrer">截图证据</a>
                <a v-if="artifactUrl(r.raw_artifact_path, r.raw_artifact_url)" :href="artifactUrl(r.raw_artifact_path, r.raw_artifact_url)" target="_blank" class="source-link" rel="noopener noreferrer">原始证据</a>
              </div>
              <div class="recent-notes warning" v-if="r.is_cited && !isEvidenceComplete(r)">
                证据不完整：缺截图或原始证据时不计入已验证引用。
              </div>
              <div class="recent-notes" v-if="r.notes && !r.is_cited">{{ r.notes.slice(0, 80) }}</div>
              <div class="recent-notes error" v-if="r.failure_reason">{{ r.failure_reason.slice(0, 100) }}</div>
            </div>
          </div>
          <div v-else class="chart-empty">暂无监测记录</div>
        </el-card>
      </div>

      <!-- 全品牌表格 -->
      <el-card class="brand-table-card">
        <template #header><span>品牌数据总览</span></template>
        <el-table :data="brandTableData" stripe max-height="350" v-loading="brandTableLoading">
          <el-table-column prop="name" label="品牌" min-width="120" />
          <el-table-column prop="industry" label="行业" width="100" />
          <el-table-column prop="keywordCount" label="关键词数" width="90" />
          <el-table-column prop="citationCount" label="已验证引用" width="110" />
          <el-table-column label="引用率" width="80">
            <template #default="{ row }">
              <span :style="{ color: row.citationRate > 50 ? '#67c23a' : '#e6a23c' }">{{ row.citationRate }}%</span>
            </template>
          </el-table-column>
          <el-table-column prop="target_platforms" label="目标平台" min-width="160">
            <template #default="{ row }">
              <el-tag v-for="p in (row.target_platforms || '').split(',').filter(Boolean)" :key="p" size="small" style="margin-right:4px;">{{ platformLabel(p.trim()) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="70">
            <template #default="{ row }">
              <el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small">{{ row.status === 'active' ? '启用' : '停用' }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<script setup>
import authFetch from '../utils/authFetch'
import { evidenceUrl } from '../utils/evidenceUrl'
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { DataAnalysis, Loading } from '@element-plus/icons-vue'
import { getBrands } from '../api/brands'
import { platformLabel } from '../utils/labels'

const selectedBrandId = ref('all')
const brands = ref([])
const stats = ref({
  totalKeywords: 0,
  publishedContent: 0,
  contentAssetCount: 0,
  publishingTaskCount: 0,
  verifiedPublishedCount: 0,
  citationCount: 0,
  verifiedCitationCount: 0,
  unverifiedCitationCount: 0,
  citationRate: 0,
  activeBrands: 0,
  totalMonitorCount: 0,
  withSourceUrl: 0,
  withScreenshot: 0,
  withAnswerText: 0,
  evidenceCompleteRate: 0,
})
const trendData = ref([])
const recentResults = ref([])
const hasData = ref(false)
const loadingData = ref(true)
const trendMode = ref('month')
const brandTableData = ref([])
const brandTableLoading = ref(false)

const selectedBrandName = computed(() => {
  const b = brands.value.find(x => x.id === selectedBrandId.value)
  return b?.name || ''
})

const trendTitle = computed(() => {
  return `监测趋势 (${trendMode.value === 'month' ? '月度' : trendMode.value === 'week' ? '周度' : '日度'})`
})

const barHeight = (count) => Math.max(count * 3, 4)

const verificationLabel = (status) => ({
  verified_citation: '已验证',
  unverified_citation: '未验证',
  not_cited: '未引用',
  failed: '失败'
}[status] || status || '未知')

const verificationTagType = (status) => ({
  verified_citation: 'success',
  unverified_citation: 'warning',
  not_cited: 'info',
  failed: 'danger'
}[status] || 'info')

const errorMessage = (error, fallback = '请求失败') => {
  return error?.detail || error?.message || fallback
}

const checkedJson = async (url, fallback) => {
  const res = await authFetch(url)
  const data = await res.json()
  if (!res.ok) throw new Error(data?.detail || fallback || '请求失败')
  return data
}

const artifactUrl = (path, precomputed) => evidenceUrl(path, precomputed)

const isEvidenceComplete = (row) => {
  return row?.source_verified && (row?.screenshot_verified || row?.raw_artifact_verified)
}

const fetchAll = async (showMessage = false) => {
  loadingData.value = true
  const isAll = selectedBrandId.value === 'all' || !selectedBrandId.value
  const brandId = isAll ? undefined : selectedBrandId.value

  try {
    // 统计
    let statsUrl
    if (isAll) statsUrl = '/api/dashboard/all-stats'
    else statsUrl = `/api/dashboard/stats?brand_id=${brandId}`
    const statsRes = await checkedJson(statsUrl, '统计接口失败')
    stats.value = statsRes

    // 趋势(按月/周/日)
    const modeMap = { month: 'monthly', week: 'weekly', day: 'daily' }
    const mode = modeMap[trendMode.value] || 'monthly'
    let trendUrl
    if (isAll) trendUrl = `/api/dashboard/all-${mode}-trend`
    else trendUrl = `/api/dashboard/${mode}-trend?brand_id=${brandId}`
    const trendRes = await checkedJson(trendUrl, '趋势接口失败')
    trendData.value = (trendRes || []).map(item => ({
      label: item.label || '',
      count: item.count || 0,
    }))

    // 最近结果(直接从monitor_results查，带source_url)
    let recentUrl
    if (isAll) recentUrl = '/api/monitor/results?limit=20'
    else recentUrl = `/api/monitor/results?brand_id=${brandId}&limit=20`
    const recentRes = await checkedJson(recentUrl, '最近结果接口失败')
      recentResults.value = (recentRes || []).map(r => ({
        keyword: r.keyword_word || r.search_query || '',
        platform: r.platform || '',
        is_cited: r.is_cited || false,
        brand_name: r.brand_name || '',
        monitored_at: r.monitored_at || '',
        source_url: r.source_url || '',
        source_domain: r.source_domain || '',
        screenshot_path: r.screenshot_path || '',
        raw_artifact_path: r.raw_artifact_path || '',
        source_verified: r.source_verified || 0,
        screenshot_verified: r.screenshot_verified || 0,
        raw_artifact_verified: r.raw_artifact_verified || 0,
        verification_status: r.verification_status || 'not_cited',
        failure_reason: r.failure_reason || '',
        notes: r.notes || '',
      }))
    hasData.value = (stats.value.totalMonitorCount || 0) > 0 ||
      recentResults.value.length > 0 ||
      trendData.value.some(item => (item.count || 0) > 0)
    if (showMessage) {
      const scope = isAll ? '全部品牌' : selectedBrandName.value
      ElMessage.success(hasData.value ? `${scope}数据已刷新` : `${scope}暂无新增数据`)
    }
  } catch (e) {
    hasData.value = false
    ElMessage.error(`数据中心加载失败: ${errorMessage(e)}`)
  } finally {
    loadingData.value = false
  }
}

const router = useRouter()
const goMonitor = () => {
  if (selectedBrandId.value && selectedBrandId.value !== 'all') {
    router.push(`/monitor?brand_id=${selectedBrandId.value}`)
  } else {
    router.push('/monitor')
  }
}

const fetchBrandTable = async () => {
  brandTableLoading.value = true
  try {
    const data = await getBrands()
    brandTableData.value = (data || []).map(b => ({
      ...b,
      target_platforms: b.target_platforms || '',
      citationRate: (b.totalMonitorCount || 0) > 0 ? ((b.citationCount || 0) / (b.totalMonitorCount || 1) * 100).toFixed(1) : '0.0',
    }))
  } catch (error) {
    ElMessage.error(`品牌总览加载失败: ${error?.message || '未知错误'}`)
  }
  finally { brandTableLoading.value = false }
}

onMounted(async () => {
  try {
    brands.value = await getBrands() || []
  } catch (error) {
    ElMessage.error(`品牌列表加载失败: ${error?.message || '未知错误'}`)
  }
  fetchAll(false)
  fetchBrandTable()
})
</script>

<style scoped>
.dashboard { padding: 20px; }
.dashboard-header { margin-bottom: 20px; display: flex; align-items: center; gap: 12px; }
.header-hint { font-size: 13px; color: #909399; }
.empty-state { padding: 80px 0; text-align: center; }
.empty-icon { color: #c0c4cc; }
.loading-text { margin-top: 12px; color: #909399; font-size: 14px; }

.kpi-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; margin-bottom: 24px; }
.kpi-card { text-align: center; padding: 22px 16px; position: relative; overflow: hidden; }
.kpi-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: var(--brand-gradient);
  opacity: 0.85;
}
.kpi-card:hover { transform: translateY(-2px); }
.kpi-value { font-size: 28px; font-weight: 700; color: var(--el-color-primary); margin-bottom: 6px; font-variant-numeric: tabular-nums; }
.kpi-blue { color: var(--el-color-primary); }
.kpi-gold { color: var(--el-color-warning); }
.kpi-green { color: var(--el-color-success); }
.kpi-label { font-size: 13px; color: var(--el-text-color-secondary); }
.evidence-strip { display: flex; gap: 8px; flex-wrap: wrap; margin: -8px 0 20px; }

.dashboard-grid { display: grid; grid-template-columns: 1.6fr 1fr; gap: 20px; margin-bottom: 20px; }
.chart-card, .recent-card { min-height: 320px; }
.card-header-row { display: flex; justify-content: space-between; align-items: center; font-weight: bold; }

.chart-area { height: 260px; display: flex; flex-direction: column; justify-content: flex-end; padding: 10px 0; }
.bar-chart { display: flex; align-items: flex-end; justify-content: space-around; height: 220px; gap: 6px; }
.bar-group { display: flex; flex-direction: column; align-items: center; flex: 1; }
.bar { width: 36px; background: linear-gradient(to top, #4c6fff, #8d70ff); border-radius: 4px 4px 0 0; display: flex; align-items: flex-start; justify-content: center; padding-top: 3px; transition: height 0.3s; min-height: 4px; max-height: 200px; box-shadow: 0 2px 6px rgba(76, 111, 255, 0.25); }
.bar-value { color: #fff; font-size: 11px; font-weight: bold; }
.bar-label { margin-top: 6px; font-size: 10px; color: #909399; white-space: nowrap; }
.chart-empty { display: flex; align-items: center; justify-content: center; height: 200px; color: #c0c4cc; }

.recent-list { display: flex; flex-direction: column; gap: 8px; max-height: 420px; overflow-y: auto; }
.recent-item { padding: 10px 12px; border-radius: 6px; background: #f5f7fa; border: 1px solid #ebeef5; }
.recent-item:hover { background: #eef2ff; border-color: #c9d4ff; }
.recent-keyword { font-size: 14px; font-weight: 500; color: #303133; margin-bottom: 4px; }
.recent-meta { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.recent-brand { font-size: 11px; color: #909399; }
.recent-time { font-size: 10px; color: #c0c4cc; margin-left: auto; white-space: nowrap; }
.recent-source { margin-top: 4px; display: flex; gap: 6px; align-items: center; }
.source-link { font-size: 12px; color: var(--el-color-primary); text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 280px; display: inline-block; }
.source-link:hover { text-decoration: underline; color: var(--el-color-primary-light-3); }
.tag-cited { flex-shrink: 0; }
.recent-notes { font-size: 11px; color: #909399; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.recent-notes.error { color: #f56c6c; }

.brand-table-card { min-height: 200px; }
</style>
