# GEO 运营系统（极优 GEO）

GEO（Generative Engine Optimization，生成式引擎优化）运营系统：监测品牌关键词在 AI 搜索中的引用情况，生成内容草稿，并分发到内容平台。

后端 FastAPI + SQLite，前端 Vue 3，浏览器自动化基于 Playwright / Chrome DevTools Protocol。

> 本项目为**本机单机运营工具**，设计前提是只在一台可信电脑上运行、只供单人使用。

---

## ⚠️ 安全提示（部署前必读）

### 1. 首次启动没有内置账号

新克隆的仓库是空库，**不存在任何预置账号**。首次启动后需在登录页点「没有账号？去注册」自行创建账号（例如 `admin / admin123`）。请把口令当成弱口令对待：

- 登录后立即在「系统设置」中修改口令
- 确认服务只监听回环地址（默认已配置为 `127.0.0.1`，见下方端口说明）
- **不要把 15173 / 18770 端口映射到公网，也不要用内网穿透暴露出去**

### 2. 系统会持有真实平台登录态

通过浏览器自动化登录抖音、小红书、知乎、百家号等平台后，浏览器 profile 目录内会留下有效的登录 Cookie。该目录位于系统用户数据目录（Windows 下为 `%APPDATA%\GeoOpsGEO\browser-profiles`），**不在本仓库内**。请勿将其复制进仓库或随包分发。

### 3. 密钥放在哪里

**内容生成大模型（首选 DeepSeek）**：在前端「系统设置 → 大模型设置」中配置，支持 DeepSeek / MiniMax / 千问 / Kimi 四家（填 API Key、启用状态、优先级，仅存本地数据库）。内容生成按优先级取第一个已启用且已填 Key 的供应商——推荐 DeepSeek 为首选，其不可用时在设置页调整优先级即可切换。

`backend/.env`（不纳入版本控制）中放的是另外几项，仓库只提供 `.env.example` 空模板，**不要把自己的 Key 提交进去**：

| 环境变量 | 用途 | 是否必需 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 千问平台 AI 监测的官方联网搜索 | 用千问做监测时必需 |
| `CAPSOLVER_API_KEY` | 验证码自动识别 | 可选 |
| `SECRET_KEY` | JWT 签名密钥 | 留空会自动生成随机密钥并写入 `backend/.secret_key` |

### 4. 合规提醒

系统的发布能力通过浏览器自动化操作第三方内容平台。使用前请自行确认各平台的用户协议与自动化条款，并确保所发布内容真实、可核验。本项目不代为承担由此产生的平台处罚或法律责任。

---

## 功能概览

| 模块 | 说明 |
|---|---|
| 品牌与关键词管理 | 维护品牌资料、行业、竞品与关键词库 |
| AI 引用监测 | 在豆包、千问、元宝等 AI 搜索中检查关键词是否被引用，留存截图与证据 |
| GEO 知识库 | 品牌事实、证据来源、问题矩阵、引用诊断四类项目级资源 |
| 策略与内容生成 | 依据知识库生成策略动作与内容草稿 |
| 发布工作流 | 草稿自动填写到各内容平台，人工点击最终发布并回填链接验真（自有官网全自动） |
| 运营大屏 | 汇总监测、内容、发布与缺口状态 |

### 发布机制说明（为什么不是「审核后全自动」）

对知乎、小红书、百家号、搜狐、头条、抖音、携程等外部平台，发布流程是：

1. 内容草稿需**人工审核**（approved）后才能创建发布任务——这一步是人工把关；
2. 系统自动打开发布页、填写标题/正文/图片/封面，并勾选「AI 生成内容」声明；
3. 最终「发布」按钮由**人工在发布浏览器中点击**。原因有二：这些平台在最终发布时普遍弹出滑块验证、短信验证码等安全验证，自动化无法可靠绕过；后端也保留了 manual_publish 门禁，明确不做「伪装成全自动」的发布；
4. 发布成功后把文章 URL 回填到系统完成验真。

唯一例外是自有官网（website 平台）：从建任务到发布验真全自动闭环。

---

## 环境要求

- Windows（主要支持平台）
- Python 3.13
- Node.js 22

其他平台（macOS/Linux）可以运行后端，但当前主要在 Windows 上验证。

---

## 快速开始

### 第 1 步：安装依赖

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium

cd ..\frontend
npm install
npm run build
```

### 第 2 步：配置（可选，留空也能启动）

```powershell
copy backend\.env.example backend\.env
```

按需在 `backend\.env` 中填入 `DASHSCOPE_API_KEY` 等（见上方安全提示第 3 条）。

### 第 3 步：启动

```powershell
cd ..
python start_services.py start
```

或直接双击 `启动GEO运营系统.bat`，停止用 `停止GEO运营系统.bat`。

### 第 4 步：注册并登录

打开 `http://localhost:15173`：

1. 登录页点「没有账号？去注册」，创建账号（用户名 + 邮箱 + 密码）
2. 用该账号登录
3. 进入「系统设置 → 大模型设置」配置生成用大模型的 API Key（推荐首选 DeepSeek，详见上方安全提示第 3 条）

> 也可用接口注册：`POST http://127.0.0.1:18770/api/auth/register`，body 为 `{"username": "...", "email": "...", "password": "..."}`。

---

## 服务管理

```powershell
# 用安装了依赖的 venv Python 执行
backend\.venv\Scripts\python.exe start_services.py start    # 启动
backend\.venv\Scripts\python.exe start_services.py status   # 查看前后端状态
backend\.venv\Scripts\python.exe start_services.py restart  # 重启（改配置后生效）
backend\.venv\Scripts\python.exe start_services.py stop     # 停止
```

- **修改代码或 `.env` 后必须 `restart` 才生效。**
- 电脑重启后：直接重新 `start`（或双击 bat），数据库与登录态保留，无需重装。
- 查看运行日志：见下方「数据都存在哪里」。

| 服务 | 默认端口 | 监听地址 |
|---|---|---|
| 前端 | 15173 | 127.0.0.1 |
| 后端 API | 18770 | 127.0.0.1 |

AI 监测与发布平台的浏览器调试端口见 `backend/.env.example`，冲突时直接改后 `restart`。

---

## 首次使用完整流程

从零跑通一遍（约 15 分钟）：

1. **建品牌**：「品牌管理」→ 新建品牌，填写品牌名、行业、官网、竞品等资料。
2. **建关键词**：「关键词库」→ 添加要监测的词（如品牌词、产品词、行业问题词）。
3. **配 AI Key**：「系统设置 → 大模型设置」填入生成用大模型 Key（首选 DeepSeek，不配则内容生成不可用）。若要用**千问**做 AI 引用监测，另需在 `backend\.env` 配 `DASHSCOPE_API_KEY`；豆包/元宝监测无需 Key。
4. **跑一次监测**：「AI 监测中心」→ 选择品牌与平台（豆包/千问/元宝）→ 启动监测。系统会打开受控浏览器逐词搜索，记录是否被引用并留存截图证据。
   - 首次会要求在弹出的浏览器里**人工登录一次**对应 AI 平台，登录态之后复用。
5. **看结果**：「数据中心」查看引用率、缺口关键词；「运营大屏」看整体态势。
6. **生成内容**：对未引用的关键词，在「运营中台」生成内容草稿（依据 GEO 知识库）。
   - 可选开启监测自动补内容：`backend\.env` 中 `GEO_AUTO_CONTENT=1`（详见 `.env.example` 注释）。
7. **发布草稿**：审核通过的草稿创建发布任务 → 系统自动打开平台并填写标题图文 → 人工在发布浏览器点击最终「发布」→ 把文章 URL 回填系统验真（自有官网全自动，见上方「发布机制说明」）。
   - 发布平台（抖音/小红书/知乎/百家号等）同样需要先人工登录一次。

日常使用只需重复 4 → 7。

---

## 目录结构

```
backend/          FastAPI 后端（路由、服务、数据模型）
frontend/         Vue 3 前端
scripts/          验收门禁与数据检查脚本
geo/              项目级 GEO 知识库与行业信源图谱
desktop-shell/    Electron 桌面壳骨架（非最终安装包）
usage_test/       冒烟与 UI 检查脚本
start_services.py 进程管理（start / stop / restart / status）
```

## 数据都存在哪里

| 数据 | 位置 | 是否入 git |
|---|---|---|
| 业务数据库 | `backend/geo_data.db` | 否（.gitignore） |
| API Key 等配置 | `backend/.env` | 否 |
| JWT 密钥 | `backend/.secret_key` | 否 |
| 运行时知识库（真实客户资料） | `geo/knowledge_base/*.json`（运行时自动生成，仓库只带 manifest 模板） | 否 |
| 浏览器登录态 | `%APPDATA%\GeoOpsGEO\browser-profiles` | 否 |
| 运行日志 / 截图 / 证据 | 系统用户数据目录（可用 `GEO_HOME` 自定义） | 否 |

换电脑迁移：备份 `geo_data.db` + `.env`（+ 浏览器 profile 目录若想保留平台登录态），放到新机同位置后 `start` 即可。

---

## 常见问题（速查）

| 现象 | 处理 |
|---|---|
| 登录页登录失败、无账号 | 新库无预置账号，先「去注册」 |
| 后端状态离线 | `start_services.py status` 查看；端口被占时改 `.env` 端口后 `restart` |
| AI 监测没有结果 | 检查大模型 Key；确认对应 AI 平台已在浏览器中登录 |
| 发布任务不能创建 | 确认目标平台已人工登录过一次（`平台连接` 状态） |
| 改了代码/配置不生效 | 必须 `restart` |

更多排查见 `USER_OPERATION_MANUAL.md` 第 11 节。

---

## 文档

- `USER_OPERATION_MANUAL.md` — 完整操作手册（环境准备、日常流程、发布、备份、FAQ）
- `GEO_OPERATING_ARCHITECTURE_RESEARCH.md` — 架构与设计思路
- `DATABASES.md` — 数据库文件说明
- `交付说明.md` — 交付范围与已知缺口

---

## 已知限制

- 外部平台发布保留人工最终确认（平台安全验证无法绕过，详见「发布机制说明」）；仅自有官网全自动。
- 桌面壳 `desktop-shell/` 仅为骨架，不能直接作为安装包分发。
- 监测质量依赖配置的模型 Key，未配置时相关功能不可用。
