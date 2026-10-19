/** Shared Chinese labels for GEO frontend (user-visible only). */

export const PLATFORM_LABELS = {
  ai: 'AI三平台',
  all: '全平台',
  web: '网页搜索',
  website: '官网/落地页',
  doubao: '豆包',
  qianwen: '千问',
  yuanbao: '元宝',
  douyin: '抖音',
  toutiao: '今日头条',
  baijiahao: '百家号',
  sohu: '搜狐号',
  zhihu: '知乎',
  xiaohongshu: '小红书',
  ctrip_travel: '携程旅拍',
}

export function platformLabel(platform) {
  if (!platform) return '未知平台'
  return PLATFORM_LABELS[platform] || platform
}

export const PUBLISH_STATUS_LABELS = {
  pending: '待处理',
  assets_ready: '素材已生成',
  prepared: '草稿已填（待人工发布）',
  running: '运行中',
  manual_required: '需人工',
  login_required: '需登录',
  failed: '失败',
  published: '已发布',
  // 发布浏览器/账号绑定状态（operations 账号 tab 的 status 列）
  logged_in: '已登录',
  browser_open: '浏览器已开·登录未确认',
  not_connected: '浏览器未开启',
  port_conflict: '端口被占用',
  port_conflict_not_closed: '端口冲突未关闭',
  closed: '已关闭',
  port_reserved: '端口被保留',
}

export function publishStatusLabel(status) {
  return PUBLISH_STATUS_LABELS[status] || status || '未知'
}

export const STRATEGY_STATUS_LABELS = {
  draft: '草稿',
  confirmed: '已确认',
  archived: '已归档',
}

export function strategyStatusLabel(status) {
  return STRATEGY_STATUS_LABELS[status] || status || '未知'
}

export const ACTION_STATUS_LABELS = {
  pending: '待确认',
  confirmed: '已确认',
  executed: '已执行',
  blocked: '待处理',
  failed: '失败',
}

export function actionStatusLabel(status) {
  return ACTION_STATUS_LABELS[status] || status || '未知'
}

export const RISK_LABELS = {
  high: '高',
  medium: '中',
  low: '低',
}

export function riskLabel(level) {
  return RISK_LABELS[level] || level || '未知'
}

export const EXECUTION_LEVEL_LABELS = {
  verified_loop: '已闭环',
  workflow_tested: '工作流已测',
  draft_ready: '草稿可用',
  candidate: '候选',
  source_only: '仅信源',
  rpa_testing: '自动填草稿测试中',
  rpa_draft: '草稿可用',
  template_only: '仅模板',
  unknown: '未知',
}

export function executionLevelLabel(level) {
  return EXECUTION_LEVEL_LABELS[level] || level || '未知'
}

export const CONTENT_TYPE_LABELS = {
  faq: 'FAQ问答',
  guide: '攻略',
  ranking: '榜单',
  review: '测评',
  qa: '问答稿',
  media: '媒体稿',
}

export function contentTypeLabel(type) {
  return CONTENT_TYPE_LABELS[type] || type || '未知'
}

export const IDEA_STATUS_LABELS = {
  pending: '待采纳',
  adopted: '已采纳',
  duplicate: '已存在',
  rejected: '已拒绝',
}

export function ideaStatusLabel(status) {
  return IDEA_STATUS_LABELS[status] || status || '未知'
}

export const KNOWLEDGE_STATUS_LABELS = {
  active: '有效',
  evidence_gap: '待补证据',
  disabled: '停用',
}

export function knowledgeStatusLabel(status) {
  return KNOWLEDGE_STATUS_LABELS[status] || status || '未知'
}

export const SESSION_STATUS_LABELS = {
  logged_in: '可用',
  api_ready: '接口可用（未登录浏览器）',
  browser_open: '浏览器已开（待自检）',
  captcha_required: '需验证',
  expired: '登录失效',
  not_connected: '未连接',
  unknown: '未知',
}

export function sessionStatusLabel(status) {
  return SESSION_STATUS_LABELS[status] || status || '未知'
}

export const FALLBACK_MODE_LABELS = {
  'auto+manual': '自动+人工',
  manual: '仅人工',
  auto: '仅自动',
  none: '无',
}

export function fallbackModeLabel(mode) {
  return FALLBACK_MODE_LABELS[mode] || mode || '未知'
}

export const CATEGORY_LABELS = {
  brand: '品牌词',
  product: '产品词',
  industry: '行业词',
  longtail: '长尾词',
  competing: '竞品词',
  scenario: '场景词',
}

export function categoryLabel(category) {
  return CATEGORY_LABELS[category] || category || '未知分类'
}
