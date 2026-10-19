<template>
  <div class="keyword-list">
    <div class="filter-bar">
      <el-select v-model="filterBrandId" placeholder="选择品牌" clearable style="width: 180px;" @change="fetchKeywords">
        <el-option v-for="brand in brands" :key="brand.id" :label="brand.name" :value="brand.id" />
      </el-select>
      <el-select v-model="filterCategory" placeholder="选择分类" clearable style="width: 130px;" @change="fetchKeywords">
        <el-option label="品牌词" value="brand" />
        <el-option label="产品词" value="product" />
        <el-option label="行业词" value="industry" />
        <el-option label="长尾词" value="longtail" />
      </el-select>
      <el-select v-model="filterAiStatus" placeholder="AI状态" clearable style="width: 130px;" @change="fetchKeywords">
        <el-option label="已验证引用" value="cited" />
        <el-option label="未验证引用" value="unverified_citation" />
        <el-option label="未引用" value="not_found" />
        <el-option label="待监测" value="pending" />
      </el-select>
      <el-button @click="fetchKeywordsWithFeedback" :loading="loading">搜索</el-button>
      <div class="filter-actions">
        <el-button type="primary" @click="openAddDialog">新增关键词</el-button>
        <el-button @click="handleBatchMonitor" :disabled="selectedIds.length === 0" type="success">
          批量监测 ({{ selectedIds.length }})
        </el-button>
        <el-button @click="handleImport" :disabled="!filterBrandId">批量导入</el-button>
        <el-button @click="downloadTemplate">下载模板</el-button>
        <el-button @click="handleExport">导出表格</el-button>
      </div>
    </div>

    <!-- Platform Citation Stats -->
    <div class="platform-stats" v-if="platformStats.length > 0">
      <span class="stats-label">按平台引用统计：</span>
      <el-tag v-for="ps in platformStats" :key="ps.platform" size="small" class="ps-tag"
        :type="ps.cited > 0 ? 'success' : 'info'">
        {{ platformMap[ps.platform] || '未知平台' }}: 已验证 {{ ps.cited }}/{{ ps.total }}，未验证 {{ ps.unverified || 0 }}
      </el-tag>
    </div>

    <el-table
      :data="keywords"
      v-loading="loading"
      stripe
      @selection-change="handleSelectionChange"
    >
      <el-table-column type="selection" width="50" />
      <el-table-column prop="word" label="关键词" min-width="160" show-overflow-tooltip />
      <el-table-column prop="category" label="分类" width="90">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ categoryMap[row.category] || '未知分类' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="search_volume" label="搜索量" width="80" align="right" />
      <el-table-column prop="ai_status" label="AI状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.ai_status === 'cited' ? 'success' : row.ai_status === 'unverified_citation' ? 'warning' : 'info'" size="small" effect="plain">
            <span v-if="row.ai_status === 'cited'">已引用</span>
            <span v-else-if="row.ai_status === 'unverified_citation'">未验证引用</span>
            <span v-else-if="row.ai_status === 'not_found'">未引用</span>
            <span v-else>-</span>
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="ai_platform" label="引用的AI平台" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="platformTagType(row.ai_platform)">
            {{ platformMap[row.ai_platform] || (row.ai_platform ? '未知平台' : '-') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="last_monitored_at" label="最后监测时间" width="170">
        <template #default="{ row }">
          <span v-if="row.last_monitored_at || row.last_tested" class="monitor-time">
            {{ row.last_monitored_at || row.last_tested }}
          </span>
          <span v-else class="muted">未监测</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openEditDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[10, 20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      @size-change="fetchKeywords"
      @current-change="fetchKeywords"
      style="margin-top: 20px; justify-content: flex-end;"
    />

    <!-- Add/Edit Dialog -->
    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="500px">
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="品牌" prop="brand_id">
          <el-select v-model="form.brand_id" placeholder="选择品牌" style="width: 100%;">
            <el-option v-for="brand in brands" :key="brand.id" :label="brand.name" :value="brand.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="关键词" prop="word">
          <el-input v-model="form.word" placeholder="请输入关键词" />
        </el-form-item>
        <el-form-item label="分类" prop="category">
          <el-select v-model="form.category" placeholder="选择分类" style="width: 100%;">
            <el-option label="品牌词" value="brand" />
            <el-option label="产品词" value="product" />
            <el-option label="行业词" value="industry" />
            <el-option label="长尾词" value="longtail" />
          </el-select>
        </el-form-item>
        <el-form-item label="搜索量" prop="search_volume">
          <el-input-number v-model="form.search_volume" :min="0" style="width: 100%;" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSubmit" :loading="submitting">确定</el-button>
      </template>
    </el-dialog>

    <!-- Import Dialog -->
    <el-dialog v-model="importDialogVisible" title="批量导入" width="500px">
      <div style="margin-bottom: 12px; font-size: 13px; color: var(--el-text-color-secondary);">
        请先在列表上方选择品牌，再上传 CSV 文件。格式请参考<a href="javascript:void(0)" @click="downloadTemplate" style="color: var(--el-color-primary);">模板文件</a>
      </div>
      <el-upload
        ref="uploadRef"
        :auto-upload="false"
        :limit="1"
        accept=".csv"
        :on-change="handleFileChange"
      >
        <template #trigger>
          <el-button>选择表格文件</el-button>
        </template>
        <template #tip>
          <div class="el-upload__tip">支持 CSV 格式文件，每行一个关键词</div>
        </template>
      </el-upload>
      <template #footer>
        <el-button @click="importDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleImportSubmit" :loading="importing">导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { getKeywords, getKeywordStats, createKeyword, updateKeyword, deleteKeyword, importKeywords, exportKeywords } from '../api/keywords'
import { getBrands } from '../api/brands'

const router = useRouter()

const categoryMap = {
  brand: '品牌词',
  product: '产品词',
  industry: '行业词',
  longtail: '长尾词'
}

const platformMap = {
  doubao: '豆包',
  yuanbao: '元宝',
  qianwen: '千问',
  web: '网页搜索'
}

const platformTagType = (platform) => {
  const map = { doubao: '', yuanbao: 'danger', qianwen: 'warning', web: 'info', pending: 'info' }
  return map[platform] || ''
}

const errorMessage = (error, fallback = '请求失败') => {
  return error?.response?.data?.detail || error?.response?.data?.message || error?.message || fallback
}

const keywords = ref([])
const brands = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const importDialogVisible = ref(false)
const submitting = ref(false)
const importing = ref(false)
const dialogTitle = ref('新增关键词')
const isEdit = ref(false)
const formRef = ref(null)
const uploadRef = ref(null)

const currentPage = ref(1)
const pageSize = ref(10)
const total = ref(0)

const filterBrandId = ref('')
const filterCategory = ref('')
const filterAiStatus = ref('')

const importFile = ref(null)

// Selection
const selectedIds = ref([])
const selectedRows = ref([])

const platformStats = ref([])

const form = reactive({
  id: null,
  brand_id: null,
  word: '',
  category: 'brand',
  search_volume: 0
})

const rules = {
  brand_id: [{ required: true, message: '请选择品牌', trigger: 'change' }],
  word: [{ required: true, message: '请输入关键词', trigger: 'blur' }],
  category: [{ required: true, message: '请选择分类', trigger: 'change' }]
}

const fetchKeywords = async () => {
  loading.value = true
  try {
    const params = {
      page: currentPage.value,
      page_size: pageSize.value
    }
    if (filterBrandId.value) params.brand_id = filterBrandId.value
    if (filterCategory.value) params.category = filterCategory.value
    if (filterAiStatus.value) params.ai_status = filterAiStatus.value
    // 兼容前端显示名称


    const data = await getKeywords(params)
    const items = data.items || data.list || data || []
    keywords.value = items
    total.value = data.total || keywords.value.length
    await fetchKeywordStats()
    if (items.length === 0) {
      ElMessage.info('未找到匹配的关键词，请调整筛选条件或先新增关键词')
    }
  } catch (error) {
    ElMessage.error(`获取关键词列表失败: ${errorMessage(error)}`)
  } finally {
    loading.value = false
  }
}

const fetchKeywordsWithFeedback = async () => {
  await fetchKeywords()
  ElMessage.success(`搜索结果已更新，共 ${total.value} 条`)
}

const fetchKeywordStats = async () => {
  const params = {}
  if (filterBrandId.value) params.brand_id = filterBrandId.value
  if (filterCategory.value) params.category = filterCategory.value
  if (filterAiStatus.value) params.ai_status = filterAiStatus.value
  try {
    const data = await getKeywordStats(params)
    platformStats.value = data?.by_platform || []
  } catch {
    platformStats.value = []
  }
}

const fetchBrands = async () => {
  try {
    const data = await getBrands()
    brands.value = data
  } catch (error) {
    ElMessage.error(`获取品牌列表失败: ${errorMessage(error)}`)
  }
}

const handleSelectionChange = (rows) => {
  selectedRows.value = rows
  selectedIds.value = rows.map(r => r.id)
}

const handleBatchMonitor = () => {
  if (selectedIds.value.length === 0) {
    ElMessage.warning('请先选择关键词')
    return
  }
  // Get the brand_id from selected rows (should all be same brand)
  const brands = new Set(selectedRows.value.map(r => r.brand_id))
  const brandId = brands.size === 1 ? [...brands][0] : (filterBrandId.value || null)

  if (!brandId) {
    ElMessage.warning('请先选择一个品牌的关键词，或通过筛选指定品牌')
    return
  }

  router.push({ path: '/monitor', query: { brand_id: brandId, keyword_ids: selectedIds.value.join(',') } })
}

const downloadTemplate = () => {
  const header = '关键词,分类,搜索量\n请替换为真实用户会问的问题,longtail,0\n请替换为真实产品或服务词,product,0\n'
  const blob = new Blob(['\ufeff' + header], { type: 'text/csv;charset=utf-8;' })
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'keywords_template.csv'
  link.click()
  window.URL.revokeObjectURL(url)
  ElMessage.success('模板下载成功')
}

const openAddDialog = () => {
  dialogTitle.value = '新增关键词'
  isEdit.value = false
  Object.assign(form, {
    id: null,
    brand_id: filterBrandId.value || (brands.value[0]?.id || null),
    word: '',
    category: 'brand',
    search_volume: 0
  })
  dialogVisible.value = true
}

const openEditDialog = (row) => {
  dialogTitle.value = '编辑关键词'
  isEdit.value = true
  Object.assign(form, {
    id: row.id,
    brand_id: row.brand_id,
    word: row.word,
    category: row.category,
    search_volume: row.search_volume
  })
  dialogVisible.value = true
}

const handleSubmit = async () => {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (valid) {
      submitting.value = true
      try {
        if (isEdit.value) {
          await updateKeyword(form.id, form)
          ElMessage.success('更新成功')
        } else {
          await createKeyword(form)
          ElMessage.success('创建成功')
        }
        dialogVisible.value = false
        fetchKeywords()
      } catch (error) {
        ElMessage.error(`${isEdit.value ? '更新失败' : '创建失败'}: ${errorMessage(error)}`)
      } finally {
        submitting.value = false
      }
    }
  })
}

const handleDelete = async (row) => {
  try {
    await deleteKeyword(row.id)
    ElMessage.success('删除成功')
    fetchKeywords()
  } catch (error) {
    ElMessage.error(`删除失败: ${errorMessage(error)}`)
  }
}

const handleImport = () => {
  if (!filterBrandId.value) {
    ElMessage.warning('请先选择品牌，再导入关键词')
    return
  }
  importFile.value = null
  importDialogVisible.value = true
}

const handleFileChange = (file) => {
  importFile.value = file.raw
}

const handleImportSubmit = async () => {
  if (!importFile.value) {
    ElMessage.warning('请选择文件')
    return
  }
  if (!filterBrandId.value) {
    ElMessage.warning('请先选择品牌，再导入关键词')
    return
  }
  importing.value = true
  try {
    const formData = new FormData()
    formData.append('file', importFile.value)
    if (filterBrandId.value) {
      formData.append('brand_id', filterBrandId.value)
    }
    const data = await importKeywords(formData)
    const skipped = data?.skipped_total || 0
    ElMessage.success(`导入成功 ${data?.total || 0} 条${skipped ? `，跳过 ${skipped} 条` : ''}`)
    importDialogVisible.value = false
    fetchKeywords()
  } catch (error) {
    ElMessage.error(`导入失败: ${errorMessage(error)}`)
  } finally {
    importing.value = false
  }
}

const handleExport = async () => {
  try {
    const params = {}
    if (filterBrandId.value) params.brand_id = filterBrandId.value
    if (filterCategory.value) params.category = filterCategory.value

    const blob = await exportKeywords(params)
    const url = window.URL.createObjectURL(new Blob([blob]))
    const link = document.createElement('a')
    link.href = url
    link.download = `keywords_${Date.now()}.csv`
    link.click()
    window.URL.revokeObjectURL(url)
    ElMessage.success('导出成功')
  } catch (error) {
    ElMessage.error(`导出失败: ${errorMessage(error)}`)
  }
}

onMounted(() => {
  fetchBrands()
  fetchKeywords()
})
</script>

<style scoped>
.keyword-list {
  padding: 20px;
  background: #fff;
  border-radius: 4px;
}

.filter-bar {
  display: flex;
  flex-wrap: wrap;
  margin-bottom: 12px;
  gap: 10px;
  align-items: center;
}

.filter-actions {
  margin-left: auto;
  display: flex;
  gap: 10px;
}

/* Platform stats */
.platform-stats {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  background: #f5f7fa;
  border-radius: 6px;
  margin-bottom: 16px;
  font-size: 13px;
}

.stats-label {
  color: #606266;
  font-weight: 500;
  white-space: nowrap;
}

.ps-tag {
  cursor: default;
}

.monitor-time {
  font-size: 13px;
  color: #606266;
}

.muted {
  color: #c0c4cc;
  font-size: 13px;
}
</style>
