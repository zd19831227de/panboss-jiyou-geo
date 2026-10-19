<template>
  <div class="login-page">
    <div class="login-bg"></div>
    <div class="login-container">
      <div class="login-card">
        <div class="login-brand">
          <div class="brand-icon">
            <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
              <defs>
                <linearGradient id="brandGrad" x1="0" y1="0" x2="48" y2="48" gradientUnits="userSpaceOnUse">
                  <stop stop-color="#4c6fff"/>
                  <stop offset="1" stop-color="#7c5cff"/>
                </linearGradient>
              </defs>
              <rect width="48" height="48" rx="12" fill="url(#brandGrad)"/>
              <path d="M24 10L24 38M16 16L24 10L32 16" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M14 28L24 38L34 28" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
              <circle cx="24" cy="20" r="3" fill="white"/>
              <circle cx="24" cy="30" r="3" fill="white"/>
            </svg>
          </div>
          <h1 class="brand-title">GEO运营系统</h1>
          <p class="brand-subtitle">全自动化GEO监测与优化平台</p>
        </div>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          class="login-form"
          @keyup.enter="handleSubmit"
        >
          <el-form-item prop="username">
            <el-input
              v-model="form.username"
              placeholder="用户名"
              size="large"
              :prefix-icon="User"
            />
          </el-form-item>

          <el-form-item prop="email" v-if="isRegister">
            <el-input
              v-model="form.email"
              placeholder="邮箱地址"
              size="large"
              :prefix-icon="Message"
            />
          </el-form-item>

          <el-form-item prop="password">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="密码"
              size="large"
              show-password
              :prefix-icon="Lock"
            />
          </el-form-item>

          <el-form-item v-if="isRegister" prop="confirmPassword">
            <el-input
              v-model="form.confirmPassword"
              type="password"
              placeholder="确认密码"
              size="large"
              show-password
              :prefix-icon="Lock"
            />
          </el-form-item>

          <div v-if="errorMsg" class="error-msg">{{ errorMsg }}</div>

          <el-button
            type="primary"
            size="large"
            :loading="loading"
            class="submit-btn"
            @click="handleSubmit"
          >
            {{ isRegister ? '注 册' : '登 录' }}
          </el-button>
        </el-form>

        <div class="login-footer">
          <el-button link type="primary" @click="toggleMode">
            {{ isRegister ? '已有账号？去登录' : '没有账号？去注册' }}
          </el-button>
        </div>

        <div class="login-version">v1.1.0</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock, Message } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const formRef = ref(null)
const isRegister = ref(false)
const loading = ref(false)
const errorMsg = ref('')

const form = reactive({
  username: '',
  email: '',
  password: '',
  confirmPassword: ''
})

const rules = computed(() => ({
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 2, message: '用户名至少2位', trigger: 'blur' }
  ],
  email: isRegister.value
    ? [
        { required: true, message: '请输入邮箱', trigger: 'blur' },
        { type: 'email', message: '邮箱格式不正确', trigger: 'blur' }
      ]
    : [],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少6位', trigger: 'blur' }
  ],
  confirmPassword: isRegister.value
    ? [
        { required: true, message: '请确认密码', trigger: 'blur' },
        {
          validator: (_, v, cb) => {
            if (v !== form.password) cb(new Error('两次密码不一致'))
            else cb()
          },
          trigger: 'blur'
        }
      ]
    : []
}))

const toggleMode = () => {
  isRegister.value = !isRegister.value
  form.password = ''
  form.confirmPassword = ''
  errorMsg.value = ''
}

const handleSubmit = async () => {
  if (!formRef.value) return
  errorMsg.value = ''
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      if (isRegister.value) {
        await auth.register(form.username, form.email, form.password)
        ElMessage.success('注册成功，请登录')
        isRegister.value = false
        form.password = ''
      } else {
        await auth.login(form.username, form.password)
        ElMessage.success('登录成功')
        router.push('/brands')
      }
    } catch (error) {
      const detail = error?.response?.data?.detail
      if (detail) {
        errorMsg.value = typeof detail === 'string' ? detail : '操作失败'
      } else {
        errorMsg.value = isRegister.value ? '注册失败，请重试' : '用户名或密码错误'
      }
    } finally {
      loading.value = false
    }
  })
}
</script>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  position: relative;
  overflow: hidden;
}

.login-bg {
  position: fixed;
  inset: 0;
  background: linear-gradient(135deg, #0d1730 0%, #16264e 45%, #1b2f5e 75%, #0d1730 100%);
  z-index: 0;
}

.login-bg::before {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse at 20% 50%, rgba(76, 111, 255, 0.14) 0%, transparent 50%),
    radial-gradient(ellipse at 80% 20%, rgba(124, 92, 255, 0.10) 0%, transparent 50%),
    radial-gradient(ellipse at 50% 80%, rgba(76, 111, 255, 0.08) 0%, transparent 50%);
}

.login-container {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  padding: 20px;
}

.login-card {
  width: 420px;
  max-width: 100%;
  padding: 48px 40px 32px;
  background: rgba(255, 255, 255, 0.98);
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3), 0 0 0 1px rgba(255, 255, 255, 0.05);
  backdrop-filter: blur(20px);
}

.login-brand {
  text-align: center;
  margin-bottom: 36px;
}

.brand-icon {
  margin-bottom: 16px;
  display: flex;
  justify-content: center;
}

.brand-title {
  font-size: 28px;
  font-weight: 700;
  color: #1a2332;
  margin: 0 0 6px 0;
  letter-spacing: 2px;
}

.brand-subtitle {
  font-size: 14px;
  color: #909399;
  margin: 0;
}

.login-form {
  margin-bottom: 8px;
}

.login-form :deep(.el-input__wrapper) {
  padding: 4px 12px;
}

.login-form :deep(.el-input__inner) {
  height: 44px;
  font-size: 14px;
}

.submit-btn {
  width: 100%;
  height: 44px;
  font-size: 15px;
  letter-spacing: 4px;
  border-radius: 8px;
  margin-top: 4px;
}

.error-msg {
  background: #fef0f0;
  color: #f56c6c;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 12px;
  text-align: center;
}

.login-footer {
  text-align: center;
  margin-top: 16px;
}

.login-version {
  text-align: center;
  margin-top: 24px;
  font-size: 12px;
  color: #c0c4cc;
}
</style>
