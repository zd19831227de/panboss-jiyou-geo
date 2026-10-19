<template>
  <div class="brand-list">
    <div class="toolbar">
      <el-button type="primary" @click="openAddDialog">新增品牌</el-button>
    </div>

    <el-table :data="brands" v-loading="loading" stripe>
      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column prop="name" label="品牌名称" />
      <el-table-column prop="industry" label="行业" />
      <el-table-column prop="target_platforms" label="目标平台">
        <template #default="{ row }">
          <el-tag v-for="platform in toArray(row.target_platforms)" :key="platform" size="small" style="margin-right: 4px;">
            {{ platformMap[platform] || platform }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small">
            {{ row.status === 'active' ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="keywordCount" label="词条数" width="100" />
      <el-table-column prop="citationCount" label="已验证引用" width="110" />
      <el-table-column prop="created_at" label="创建时间" width="180" />
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openEditDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="680px">
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="品牌名称" prop="name">
          <el-input v-model="form.name" placeholder="请输入品牌名称" />
        </el-form-item>
        <el-form-item label="品牌别名">
          <el-input v-model="form.aliases" type="textarea" :rows="2" placeholder="AI 回答里常用的简称/别称，多个用逗号分隔，如 全称简称都填。命中别名也算“提到品牌”" />
        </el-form-item>
        <el-form-item label="行业" prop="industry">
          <el-input v-model="form.industry" placeholder="请输入行业" />
        </el-form-item>
        <el-form-item label="产品服务">
          <el-input v-model="form.products" type="textarea" :rows="2" placeholder="多个用逗号分隔，如高空江景房,亲子房,会议服务" />
        </el-form-item>
        <el-form-item label="目标人群">
          <el-input v-model="form.target_audience" type="textarea" :rows="2" placeholder="多个用逗号分隔，如商务差旅,情侣出行,亲子家庭" />
        </el-form-item>
        <el-form-item label="核心卖点">
          <el-input v-model="form.selling_points" type="textarea" :rows="2" placeholder="多个用逗号分隔，如江景,近地铁,早餐,服务好" />
        </el-form-item>
        <el-form-item label="竞品">
          <el-input v-model="form.competitors" type="textarea" :rows="2" placeholder="多个用逗号分隔，用于生成对比词和成绩单诊断" />
        </el-form-item>
        <el-form-item label="自有来源">
          <el-input v-model="form.owned_sites" type="textarea" :rows="2" placeholder="官网、OTA、地图、媒体稿等可引用链接，多个用逗号分隔" />
        </el-form-item>
        <el-form-item label="目标平台" prop="target_platforms">
          <el-checkbox-group v-model="form.target_platforms">
            <el-checkbox value="doubao">豆包</el-checkbox>
            <el-checkbox value="yuanbao">元宝</el-checkbox>
            <el-checkbox value="qianwen">千问</el-checkbox>
            <el-checkbox value="web">网页搜索</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="状态" prop="status">
          <el-radio-group v-model="form.status">
            <el-radio label="active">启用</el-radio>
            <el-radio label="inactive">停用</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSubmit" :loading="submitting">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getBrands, createBrand, updateBrand, deleteBrand } from '../api/brands'

const platformMap = {
  doubao: '豆包',
  yuanbao: '元宝',
  qianwen: '千问',
  web: '网页搜索'
}
const allowedPlatforms = new Set(Object.keys(platformMap))

const errorMessage = (error, fallback = '请求失败') => {
  return error?.response?.data?.detail || error?.response?.data?.message || error?.message || fallback
}

const brands = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const submitting = ref(false)
const dialogTitle = ref('新增品牌')
const isEdit = ref(false)
const formRef = ref(null)

const form = reactive({
  id: null,
  name: '',
  aliases: '',
  industry: '',
  products: '',
  target_audience: '',
  selling_points: '',
  competitors: '',
  owned_sites: '',
  target_platforms: [],
  status: 'active'
})

const rules = {
  name: [{ required: true, message: '请输入品牌名称', trigger: 'blur' }],
  // industry is optional - no required rule
  target_platforms: [{ required: true, message: '请选择目标平台', trigger: 'change' }]
}

const fetchBrands = async () => {
  loading.value = true
  try {
    const data = await getBrands()
    brands.value = data.map(brand => ({
      ...brand,
      keywordCount: brand.keywordCount || 0,
      citationCount: brand.citationCount || 0
    }))
  } catch (error) {
    ElMessage.error(`获取品牌列表失败: ${errorMessage(error)}`)
  } finally {
    loading.value = false
  }
}

const toArray = (val) => {
  if (!val) return []
  if (Array.isArray(val)) return val
  if (typeof val === 'string') return val.split(',').filter(Boolean)
  return []
}

const normalizePlatforms = (val) => {
  return toArray(val)
    .map(item => (
      ['douyin', 'xiaohongshu', 'zhihu', 'toutiao', 'sohu', 'baijiahao', 'ctrip_travel', 'website'].includes(item)
        ? 'web'
        : item
    ))
    .filter(item => allowedPlatforms.has(item))
    .filter((item, index, arr) => arr.indexOf(item) === index)
}

const openAddDialog = () => {
  dialogTitle.value = '新增品牌'
  isEdit.value = false
  Object.assign(form, {
    id: null,
    name: '',
    aliases: '',
    industry: '',
    products: '',
    target_audience: '',
    selling_points: '',
    competitors: '',
    owned_sites: '',
    target_platforms: [],
    status: 'active'
  })
  dialogVisible.value = true
}

const openEditDialog = (row) => {
  dialogTitle.value = '编辑品牌'
  isEdit.value = true
  Object.assign(form, {
    id: row.id,
    name: row.name,
    aliases: row.aliases || '',
    industry: row.industry || '',
    products: row.products || '',
    target_audience: row.target_audience || '',
    selling_points: row.selling_points || '',
    competitors: row.competitors || '',
    owned_sites: row.owned_sites || '',
    target_platforms: normalizePlatforms(row.target_platforms),
    status: row.status
  })
  dialogVisible.value = true
}

const handleSubmit = async () => {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (valid) {
      submitting.value = true
      try {
        // Convert target_platforms array to comma-separated string for backend
        const payload = {
          name: form.name,
          aliases: form.aliases,
          industry: form.industry,
          products: form.products,
          target_audience: form.target_audience,
          selling_points: form.selling_points,
          competitors: form.competitors,
          owned_sites: form.owned_sites,
          target_platforms: Array.isArray(form.target_platforms) ? form.target_platforms.join(',') : form.target_platforms,
          status: form.status
        }

        if (isEdit.value) {
          await updateBrand(form.id, payload)
          ElMessage.success('更新成功')
        } else {
          await createBrand(payload)
          ElMessage.success('创建成功')
        }
        dialogVisible.value = false
        fetchBrands()
      } catch (error) {
        const msg = errorMessage(error, '')
        ElMessage.error((isEdit.value ? '更新失败' : '创建失败') + (msg ? ': ' + msg : ''))
      } finally {
        submitting.value = false
      }
    }
  })
}

const handleDelete = (row) => {
  ElMessageBox.confirm(`确定要删除品牌「${row.name}」吗？`, '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning'
  }).then(async () => {
    try {
      await deleteBrand(row.id)
      ElMessage.success('删除成功')
      fetchBrands()
    } catch (error) {
      ElMessage.error(`删除失败: ${errorMessage(error)}`)
    }
  }).catch(() => {})
}

onMounted(() => {
  fetchBrands()
})
</script>

<style scoped>
.brand-list {
  padding: 20px;
  background: #fff;
  border-radius: 4px;
}

.toolbar {
  margin-bottom: 20px;
}
</style>
