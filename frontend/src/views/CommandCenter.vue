<template>
  <div class="command-center">
    <div class="topbar">
      <div>
        <div class="eyebrow">GEO 运营指挥舱</div>
        <h2>真实监测证据大屏</h2>
      </div>
      <div class="toolbar">
        <el-select v-model="selectedBrandId" size="small" style="width: 240px" @change="fetchData">
          <el-option label="全部品牌" value="all" />
          <el-option v-for="brand in brands" :key="brand.id" :label="brand.name" :value="brand.id" />
        </el-select>
        <span>更新时间 {{ nowText }}</span>
        <el-button size="small" type="primary" @click="fetchData" :loading="loading">刷新</el-button>
        <el-button size="small" @click="goMonitor">运行监测</el-button>
      </div>
    </div>

    <div class="kpis">
      <div class="kpi">
        <span>已验证引用率</span>
        <strong>{{ overview.verified_rate || 0 }}%</strong>
        <small>{{ overview.verified || 0 }} / {{ overview.total_monitor || 0 }}</small>
      </div>
      <div class="kpi">
        <span>来源证据率</span>
        <strong>{{ overview.evidence_rate || 0 }}%</strong>
        <small>来源链接 {{ overview.with_source || 0 }}</small>
      </div>
      <div class="kpi">
        <span>平台可用</span>
        <strong>{{ overview.usable_platforms || 0 }}/{{ overview.platform_count || 0 }}</strong>
        <small>豆包 / 千问 / 元宝</small>
      </div>
      <div class="kpi">
        <span>截图证据</span>
        <strong>{{ overview.with_screenshot || 0 }}</strong>
        <small>回答原文 {{ overview.with_answer || 0 }}</small>
      </div>
      <div class="kpi danger">
        <span>失败/待处理</span>
        <strong>{{ overview.failed || 0 }}</strong>
        <small>未验证 {{ overview.unverified || 0 }}</small>
      </div>
      <div class="kpi">
        <span>内容运营资产</span>
        <strong>{{ overview.content_assets || 0 }}</strong>
        <small>关键词建议 {{ overview.keyword_ideas || 0 }}</small>
      </div>
      <div class="kpi">
        <span>发布闭环</span>
        <strong>{{ overview.verified_publish_tasks || 0 }}/{{ overview.current_publish_tasks || 0 }}</strong>
        <small>历史失败 {{ overview.historical_failed_publish_tasks || 0 }} · 成绩单 {{ overview.geo_reports || 0 }}</small>
      </div>
    </div>

    <div class="grid main-grid">
      <section class="panel wide">
        <div class="panel-title">14 日监测走势</div>
        <div class="trend">
          <div v-for="item in dailyTrend" :key="item.label" class="trend-item">
            <div class="bars">
              <i class="bar total" :style="{ height: trendHeight(item.total) }"></i>
              <i class="bar verified" :style="{ height: trendHeight(item.verified) }"></i>
              <i class="bar failed" :style="{ height: trendHeight(item.failed) }"></i>
            </div>
            <span>{{ shortDate(item.label) }}</span>
          </div>
        </div>
        <div class="legend">
          <span><i class="dot total"></i>监测</span>
          <span><i class="dot verified"></i>已验证</span>
          <span><i class="dot failed"></i>失败</span>
        </div>
      </section>

      <section class="panel">
        <div class="panel-title">平台登录态</div>
        <div class="session-list">
          <div v-for="item in sessionRows" :key="item.platform" class="session-row">
            <span>{{ platformName(item.platform) }}</span>
            <el-tag :type="sessionType(item.status)" size="small">{{ sessionLabel(item.status) }}</el-tag>
            <small>端口 {{ item.port }}</small>
          </div>
        </div>
      </section>
    </div>

    <div class="grid">
      <section class="panel">
        <div class="panel-title">平台表现</div>
        <div class="metric-list">
          <div v-for="item in platformStats" :key="item.platform" class="metric-row">
            <div class="metric-head">
              <span>{{ platformName(item.platform) }}</span>
              <b>{{ item.verified_rate }}%</b>
            </div>
            <div class="meter"><i :style="{ width: `${item.verified_rate || 0}%` }"></i></div>
            <small>总 {{ item.total }} / 已验证 {{ item.verified || 0 }} / 来源 {{ item.with_source || 0 }} / 失败 {{ item.failed || 0 }}</small>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-title">来源域名排行</div>
        <div class="rank-list">
          <div v-for="(item, index) in sourceDomains" :key="item.domain" class="rank-row">
            <b>{{ index + 1 }}</b>
            <span>{{ item.domain }}</span>
            <em>{{ item.count }}</em>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-title">失败原因</div>
        <div class="rank-list">
          <div v-for="(item, index) in failureReasons" :key="`${item.platform}-${item.reason}`" class="rank-row">
            <b>{{ index + 1 }}</b>
            <span>{{ item.reason }}</span>
            <em>{{ platformName(item.platform) }} {{ item.count }}</em>
          </div>
          <div v-if="!failureReasons.length" class="empty">暂无失败记录</div>
        </div>
      </section>
    </div>

    <div class="grid bottom-grid">
      <section class="panel">
        <div class="panel-title">品牌表现排行</div>
        <div class="brand-list">
          <div v-for="item in brandRanking" :key="item.id" class="brand-row">
            <div>
              <b>{{ item.name }}</b>
              <small>{{ item.industry || '未设置行业' }} · 关键词 {{ item.keywords || 0 }}</small>
            </div>
            <div class="brand-score">
              <strong>{{ item.verified_rate }}%</strong>
              <small>证据 {{ item.evidence_rate }}%</small>
            </div>
          </div>
        </div>
      </section>

      <section class="panel wide">
        <div class="panel-title">最新真实监测流</div>
        <div class="result-feed">
          <div v-for="item in latestResults" :key="item.id" class="feed-row">
            <el-tag size="small" :type="verificationType(item.verification_status)">{{ verificationLabel(item.verification_status) }}</el-tag>
            <span class="feed-main">{{ item.brand_name }} · {{ item.keyword || item.search_query }}</span>
            <span>{{ platformName(item.platform) }}</span>
            <a v-if="item.source_verified && item.source_url" :href="item.source_url" target="_blank" rel="noopener noreferrer">{{ item.source_domain || '来源' }}</a>
            <small>{{ item.monitored_at }}</small>
          </div>
        </div>
      </section>
    </div>

    <div class="grid bottom-grid">
      <section class="panel">
        <div class="panel-title">发布状态分布</div>
        <div class="rank-list">
          <div v-for="item in publishingStatus" :key="item.status" class="rank-row">
            <b>{{ publishLabel(item.status).slice(0, 2) }}</b>
            <span>{{ publishLabel(item.status) }}</span>
            <em>{{ item.count }}</em>
          </div>
          <div v-if="!publishingStatus.length" class="empty">暂无发布任务</div>
        </div>
      </section>

      <section class="panel wide">
        <div class="panel-title">最新发布闭环</div>
        <div class="result-feed">
          <div v-for="item in latestPublishTasks" :key="item.id" class="feed-row">
            <el-tag size="small" :type="publishType(item.status)">{{ publishLabel(item.status) }}</el-tag>
            <span class="feed-main">{{ item.brand_name }} · {{ item.title }}</span>
            <span>{{ platformName(item.platform) }}</span>
            <a v-if="artifactUrl(item.raw_artifact_path, item.raw_artifact_url)" :href="artifactUrl(item.raw_artifact_path, item.raw_artifact_url)" target="_blank">网页原文</a>
            <a v-if="artifactUrl(item.screenshot_path, item.screenshot_url)" :href="artifactUrl(item.screenshot_path, item.screenshot_url)" target="_blank">截图</a>
            <a v-if="item.published_url_verified && item.published_url" :href="item.published_url" target="_blank" rel="noopener noreferrer">发布页</a>
            <small>{{ item.updated_at }}</small>
          </div>
          <div v-if="!latestPublishTasks.length" class="empty">暂无发布任务</div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import authFetch from '../utils/authFetch'
import { evidenceUrl } from '../utils/evidenceUrl'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { getBrands } from '../api/brands'

const router = useRouter()
const loading = ref(false)
const data = ref({})
const nowText = ref('')
const brands = ref([])
const selectedBrandId = ref('all')

const overview = computed(() => data.value.overview || {})
const platformStats = computed(() => data.value.platform_stats || [])
const sourceDomains = computed(() => data.value.source_domains || [])
const failureReasons = computed(() => data.value.failure_reasons || [])
const brandRanking = computed(() => data.value.brand_ranking || [])
const latestResults = computed(() => data.value.latest_results || [])
const dailyTrend = computed(() => data.value.daily_trend || [])
const publishingStatus = computed(() => data.value.publishing_status || [])
const latestPublishTasks = computed(() => data.value.latest_publish_tasks || [])
const sessionRows = computed(() => Object.entries(data.value.platform_sessions || {}).map(([platform, value]) => ({ platform, ...value })))

const maxTrend = computed(() => Math.max(1, ...dailyTrend.value.map(item => item.total || 0)))
const trendHeight = (value) => `${Math.max(4, Math.round((value || 0) / maxTrend.value * 118))}px`
const shortDate = (value) => (value || '').slice(5)

const platformName = (platform) => ({
  doubao: '豆包',
  qianwen: '千问',
  yuanbao: '元宝',
  web: '网页搜索',
  website: '官网',
  ctrip_travel: '携程旅拍',
  douyin: '抖音',
  toutiao: '今日头条',
  baijiahao: '百家号',
  sohu: '搜狐号',
  zhihu: '知乎',
  xiaohongshu: '小红书',
})[platform] || platform

const sessionLabel = (status) => ({
  logged_in: '可用',
  api_ready: '接口可用（未登录浏览器）',
  browser_open: '浏览器已开（待自检）',
  captcha_required: '需验证',
  not_connected: '未连接',
  expired: '登录失效',
  unknown: '未知',
})[status] || status || '未知'

const sessionType = (status) => ({
  logged_in: 'success',
  api_ready: 'warning',
  browser_open: 'warning',
  captcha_required: 'danger',
  not_connected: 'info',
  expired: 'danger',
})[status] || 'info'

const verificationLabel = (status) => ({
  verified_citation: '已验证',
  unverified_citation: '未验证',
  not_cited: '未引用',
  failed: '失败',
})[status] || status || '未知'

const verificationType = (status) => ({
  verified_citation: 'success',
  unverified_citation: 'warning',
  not_cited: 'info',
  failed: 'danger',
})[status] || 'info'

const publishLabel = (status) => ({
  pending: '待处理',
  assets_ready: '素材已生成',
  prepared: '草稿已填（待人工发布）',
  running: '运行中',
  published: '已发布',
  login_required: '需登录',
  manual_required: '需人工',
  failed: '失败',
  draft: '草稿',
})[status] || status || '未知'

const publishType = (status) => ({
  assets_ready: 'info',
  prepared: 'warning',
  published: 'success',
  running: 'warning',
  login_required: 'warning',
  manual_required: 'warning',
  failed: 'danger',
})[status] || 'info'

const artifactUrl = (path, precomputed) => evidenceUrl(path, precomputed)

const fetchData = async () => {
  loading.value = true
  try {
    const query = selectedBrandId.value && selectedBrandId.value !== 'all' ? `?brand_id=${selectedBrandId.value}` : ''
    const res = await authFetch(`/api/dashboard/command-center${query}`)
    const payload = await res.json()
    if (!res.ok) throw new Error(payload?.detail || payload?.message || '大屏接口失败')
    data.value = payload
    nowText.value = new Date().toLocaleString()
  } catch (e) {
    ElMessage.error(`大屏数据加载失败: ${e?.message || '未知错误'}`)
  } finally {
    loading.value = false
  }
}

const goMonitor = () => {
  if (selectedBrandId.value && selectedBrandId.value !== 'all') router.push(`/monitor?brand_id=${selectedBrandId.value}`)
  else router.push('/monitor')
}

onMounted(async () => {
  try {
    brands.value = await getBrands() || []
  } catch (e) {
    ElMessage.error(`品牌列表加载失败: ${e?.message || '未知错误'}`)
  }
  await fetchData()
})
</script>

<style scoped>
.command-center { min-height: calc(100vh - 40px); padding: 20px; background: #0f1720; color: #e5edf5; }
.topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; }
.eyebrow { color: #7dd3fc; font-size: 12px; margin-bottom: 4px; }
h2 { font-size: 24px; font-weight: 700; margin: 0; }
.toolbar { display: flex; align-items: center; gap: 10px; color: #9fb0c3; font-size: 12px; }
.kpis { display: grid; grid-template-columns: repeat(7, 1fr); gap: 12px; margin-bottom: 12px; }
.kpi, .panel { background: #152331; border: 1px solid #284157; border-radius: 8px; }
.kpi { padding: 16px; min-height: 110px; display: flex; flex-direction: column; justify-content: space-between; }
.kpi span, .kpi small { color: #9fb0c3; }
.kpi strong { font-size: 30px; color: #7dd3fc; }
.kpi.danger strong { color: #fb7185; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 12px; }
.main-grid, .bottom-grid { grid-template-columns: 2fr 1fr; }
.wide { grid-column: span 1; }
.panel { padding: 14px; min-height: 250px; overflow: hidden; }
.panel-title { font-size: 15px; font-weight: 700; margin-bottom: 12px; color: #f8fafc; }
.trend { height: 160px; display: flex; align-items: flex-end; gap: 10px; border-bottom: 1px solid #284157; padding: 0 4px 8px; }
.trend-item { flex: 1; min-width: 24px; display: flex; flex-direction: column; align-items: center; gap: 6px; color: #9fb0c3; font-size: 11px; }
.bars { height: 124px; display: flex; align-items: flex-end; gap: 3px; }
.bar { width: 7px; border-radius: 4px 4px 0 0; display: block; }
.bar.total, .dot.total { background: #38bdf8; }
.bar.verified, .dot.verified { background: #22c55e; }
.bar.failed, .dot.failed { background: #fb7185; }
.legend { display: flex; gap: 14px; margin-top: 10px; color: #9fb0c3; font-size: 12px; }
.dot { width: 8px; height: 8px; display: inline-block; border-radius: 50%; margin-right: 5px; }
.session-list, .metric-list, .rank-list, .brand-list, .result-feed { display: flex; flex-direction: column; gap: 8px; }
.session-row, .rank-row, .brand-row, .feed-row { display: flex; align-items: center; gap: 8px; padding: 8px; background: #0f1b27; border-radius: 6px; }
.session-row span:first-child { min-width: 44px; }
.session-row small, .metric-row small, .brand-row small, .feed-row small { color: #8ba0b5; }
.metric-row { padding: 8px; background: #0f1b27; border-radius: 6px; }
.metric-head { display: flex; justify-content: space-between; margin-bottom: 6px; }
.metric-head b { color: #7dd3fc; }
.meter { height: 7px; background: #203345; border-radius: 99px; overflow: hidden; margin-bottom: 6px; }
.meter i { display: block; height: 100%; background: #22c55e; }
.rank-row b { width: 22px; color: #7dd3fc; }
.rank-row span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rank-row em { color: #cbd5e1; font-style: normal; font-size: 12px; }
.brand-row { justify-content: space-between; }
.brand-row b { display: block; max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.brand-score { text-align: right; }
.brand-score strong { color: #22c55e; }
.feed-row { font-size: 12px; }
.feed-main { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.feed-row a { color: #7dd3fc; text-decoration: none; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty { color: #8ba0b5; padding: 20px 0; text-align: center; }
@media (max-width: 1200px) {
  .kpis, .grid, .main-grid, .bottom-grid { grid-template-columns: 1fr; }
}
</style>
