# GEO运营系统 操作手册

**本机（Windows）适用目录**：`C:\path\to\GEO运营系统`  
历史 macOS 路径仅作备份参考，日常请用上面的 Windows 路径。

GitHub 仓库：`https://github.com/zd19831227/panboss-jiyou-geo`

> 更新：2026-07-24 — Windows 端口改为前端 `15173` / 后端 `18770`；发布为半自动（填草稿 + 人工点发布）。

## 1. 系统包含什么

本文件夹内包含系统代码、启动脚本、验收脚本和交付文档：

- `backend/`：FastAPI 后端、SQLite 数据模型、任务队列、AI 监测、发布 RPA。
- `frontend/`：Vue 3 前端页面。
- `scripts/`：商业验收、证据检查、重启恢复检查。
- `start_services.py`：一键启动/停止/状态检查。
- `desktop-shell/`：桌面壳骨架，用于后续封装，不是最终安装包。
- `启动GEO运营系统.command` / `.bat`：macOS / Windows 启动器。
- `COMMERCIAL_DELIVERY_STATUS.md`：商业交付状态。
- `THIRD_PARTY_AUDIT_HANDOFF.md`：给第三方 agent 的审计交接。
- `RUNTIME_RECOVERY_STATUS.md`：最近一次恢复检查报告。

以下文件只保存在本机，不上传 GitHub：

- `backend/.env`：密钥配置。
- `backend/geo_data.db`：真实业务数据库。
- `frontend/node_modules/`、`backend/.venv/`：依赖环境。
- `output/`、`.playwright-cli/`：浏览器测试和证据输出。

如果压缩包解压后看到两个目录，真正项目目录是：

- `GEO运营系统`

如果还有 `__MACOSX`，那是 macOS 压缩附带目录，可以删除。

## 2. 首次环境准备

### Windows（本机推荐）

后端（PowerShell）：

```powershell
cd C:\path\to\GEO运营系统
py -3.13 -m venv backend\.venv
backend\.venv\Scripts\pip.exe install -r backend\requirements.txt
backend\.venv\Scripts\python.exe -m playwright install chromium
```

前端：

```powershell
cd C:\path\to\GEO运营系统\frontend
npm install
```

可选配置：复制 `backend\.env.example` 为 `backend\.env`（若存在），或新建空 `.env`。

### macOS（历史备份机）

```bash
cd /path/to/GEO运营系统
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
backend/.venv/bin/python -m playwright install chromium
cd frontend && npm install
```

`backend/.env` 可配置：

```bash
CAPSOLVER_API_KEY=你的Capsolver Key
DASHSCOPE_API_KEY=你的千问百炼Key   # 仅用于千问平台的AI监测联网搜索
SECRET_KEY=请换成生产随机密钥
```

> 内容生成用的大模型 Key（**首选 DeepSeek**，另有 MiniMax/千问/Kimi）在前端「系统设置 → 大模型设置」中配置，不放在 `.env`。

## 3. 启动和停止

推荐双击（Windows）：

- `启动GEO运营系统.bat`
- `停止GEO运营系统.bat`

或命令行：

```powershell
cd C:\path\to\GEO运营系统
backend\.venv\Scripts\python.exe start_services.py
backend\.venv\Scripts\python.exe start_services.py status
backend\.venv\Scripts\python.exe start_services.py stop
```

访问（本机 Windows 端口，已避开其他项目冲突）：

- 前端：`http://localhost:15173`
- 后端：`http://127.0.0.1:18770`
- 默认账号：首次启动**无预置账号**，在登录页点「没有账号？去注册」创建（示例 `admin / admin123`）

> ⚠️ **安全提示**：`admin / admin123` 是便于演示的固定弱口令，仅限本机自用。请登录后立即在「系统设置」改密；不要将 15173 / 18770 端口暴露到公网或内网穿透（服务默认只监听 `127.0.0.1`）。详见仓库根目录 `README.md` 的「安全提示」。

说明：

- `15173` 是前端界面。
- `18770` 是本地 API，前端所有按钮都靠它读写数据、调用任务、返回证据文件。
- 旧文档里的 `5173/8770` 仅为历史 macOS 端口，本机不要用。

### 3.1 端口可以改（2026-09-15 起）

以上端口（以及豆包/千问/元宝、各发布平台的浏览器端口）**都可以配置**，不用改代码。

复制 `backend\.env.example` 为 `backend\.env`，按需修改后重启服务即可：

```ini
GEO_BACKEND_PORT=18770
GEO_FRONTEND_PORT=15173
GEO_PLATFORM_PORTS=doubao:9321,qianwen:9322,yuanbao:9323
GEO_PUBLISH_PORTS=website:9521,ctrip_travel:9522,baijiahao:9523,sohu:9524,zhihu:9525,xiaohongshu:9526,douyin:9527,toutiao:9528
```

改完执行：

```powershell
backend\.venv\Scripts\python.exe start_services.py restart
backend\.venv\Scripts\python.exe start_services.py status
```

**端口被别的程序占用时**（本机 9521/9522 被其它程序占用），把发布端口挪开即可。
**本机已于 2026-09-15 改成 8521/8522**：

```ini
GEO_PUBLISH_PORTS=website:8521,ctrip_travel:8522,baijiahao:9523,sohu:9524,zhihu:9525,xiaohongshu:9526,douyin:9527,toutiao:9528
```

规则：不要用 `9221-9229` / `9421-9429`；发布端口之间不能重复，也不能和 AI 监测端口重复。
详见 `docs\端口配置与修复说明_20260915.md`。

## 4. 电脑重启后的恢复流程

电脑重启后，服务进程和浏览器进程会关闭，但固定 profile 登录态不会删除。

按顺序操作：

1. 运行 `backend\.venv\Scripts\python.exe start_services.py`。
2. 打开 `http://localhost:15173`。
3. 登录 `admin / admin123`。
4. 进入“系统设置”。
5. 检查豆包、千问、元宝状态。
6. 如果平台不是“可用”，点“打开登录”，人工登录或处理验证码。
7. 分别点三个平台的“运行自检”。
8. 运行：

```powershell
backend\.venv\Scripts\python.exe scripts\restart_recovery_check.py
```

结果必须是：

```json
"status": "ready"
```

如果不是 `ready`，按报告里的“必须处理”修复。

当前版本已改为跨平台运行目录。运行时文件默认放到系统用户目录下的应用运行目录，不再强依赖旧 `/tmp/hermes/...`。浏览器登录态会优先复用旧 `~/.geo-ops-browser`，避免升级后把现有登录态冲掉。

## 5. AI 平台登录态

AI 监测使用固定端口和固定 profile：

- 豆包：端口 `9321`
- 千问：端口 `9322`
- 元宝：端口 `9323`

规则：

- 不使用 Hermes 的 `9221-9229` 端口。
- 关闭浏览器不会删除登录态。
- 重启电脑后需要重新打开浏览器进程。
- 平台弹验证码时，系统必须标记为不可用或需验证，不能伪装成功。

发布浏览器端口固定为：

- website：`9521`
- ctrip_travel：`9522`
- baijiahao：`9523`
- sohu：`9524`
- zhihu：`9525`
- xiaohongshu：`9526`
- douyin：`9527`
- toutiao：`9528`

这些端口不能与 AI 监测端口混用。

## 6. 日常使用流程

### 6.1 品牌管理

进入“品牌管理”：

1. 新增品牌。
2. 填品牌名称、官网、自有域名、竞品等资料。
3. 保存后检查列表是否出现。

品牌是关键词、监测、内容生产、成绩单的主数据。

### 6.2 关键词库

进入“关键词库”：

- 品牌词：用户直接搜索品牌或酒店名。
- 产品词：用户搜索具体产品、房型、服务、卖点。
- 行业词：用户搜索行业通用需求，如“本地酒店推荐”。
- 长尾词：更接近真实提问，如“市中心步行街附近高空景观酒店推荐”。

AI 状态：

- 已验证引用：AI 回答提到品牌，并且有来源 URL。
- 未验证引用：AI 回答提到品牌，但没有可验证来源 URL。
- 未引用：AI 回答未提到品牌。
- 待监测：还没有真实检测。

### 6.3 AI 监测中心

进入“AI 监测中心”：

1. 选择品牌。
2. 选择关键词。
3. 选择平台：豆包、千问、元宝或 AI 三平台。
4. 提交监测。
5. 等任务完成。
6. 查看回答文本、引用状态、来源链接、截图、失败原因。

注意：

- 没有来源 URL 的结果不能算“已验证引用”。
- 有失败原因是正常商业行为，不能被隐藏。
- 如果平台验证码或登录失效，需要回系统设置处理。

### 6.4 数据中心

进入“数据中心”：

- 按日/周/月查看趋势。
- 按品牌筛选。
- 查看最近检测结果。
- 查看证据完整率。

判断口径：

- 总监测数来自真实 DB。
- AI 引用数来自真实监测结果。
- 来源链接数来自 `monitor_sources` 或结果 source URL。
- 截图数来自真实截图文件。

### 6.5 运营大屏

进入“运营大屏”：

- 查看监测概览。
- 查看平台状态。
- 查看来源生态。
- 查看失败原因。
- 查看运营资产统计。

大屏用于经营判断，不用于伪造业绩。没有证据的数据不能当成果。

### 6.6 运营中台

进入“运营中台”：

1. 生成运营关键词。
2. 采纳关键词到关键词库。
3. 生成内容。
4. 做质量检查。
5. 审核内容。
6. 创建发布任务。
7. 打开发布平台。
8. RPA 辅助填充草稿。
9. 人工最终确认发布。
10. 回填真实 URL。
11. 复测 URL。
12. 生成成绩单。

发布原则：

- 当前不是无人值守自动发布。
- 系统只做固定 URL 打开、草稿填充、证据保存、URL 回填和复测。
- 最终发布点击必须由人工确认。
- 没有真实 URL 不能算发布成功。
- 官网/落地页的「自动验真」生成的是**本机链接**，按上一条原则**不能算发布成功**，只算「素材就绪」；要算发布成功必须人工部署到真实站点并回填公网 URL。
- 知乎、携程旅拍、头条、搜狐、小红书都已有真实闭环样本。
- 携程旅拍最终验真使用携程 C 端公开页，不使用内容中心后台详情页冒充公开链接。
- 抖音历史 `task339` 已如实降级失败，不能再当成功样本。

## 7. 发布平台

当前已闭环或已有真实闭环样本的平台：

- 官网/落地页：`website` —— ⚠️ **2026-09-19 复核：其「自动验真」只验本机回环页（`127.0.0.1` 的 `/artifacts/...`），属系统自测，不代表公网发布、不计入对外成果。** 真实官网发布需人工部署后回填公网 URL。详见 `WEBSITE_LOOP_TRUTH_AUDIT_20260919.md`
- 携程旅拍：`ctrip_travel`
- 知乎：`zhihu`
- 头条号：`toutiao`
- 搜狐号：`sohu`
- 小红书：`xiaohongshu`

当前待继续真机验收的平台：

- 百家号：`baijiahao`
- 抖音创作者中心：`douyin`

当前禁用平台：

- 公众号：`wechat`
- 西瓜视频：`xigua`
- 企鹅号：`penguin`

发布浏览器用完要关闭，避免占用内存，但不要删除 profile 目录。

## 8. 验收门禁

每次大改后运行：

```bash
python3 scripts/commercial_acceptance.py
```

必须通过：

- backend health
- frontend build
- data integrity
- evidence gate
- publishing workflow gate
- operations gate
- production gate
- full evidence audit
- commercial acceptance

重启恢复门禁：

```bash
python3 scripts/restart_recovery_check.py
```

前端构建：

```bash
npm --prefix frontend run build
```

当前真实边界：

- Windows 本机可日常运行（启动 / 监测 / 半自动发布草稿 / UI）。
- 商业验收见 `usage_test/验收收尾状态说明_20260723.md`（11/12 绿；`production_gate` 为监测业务数据缺口）。
- 外部发布平台（抖音/小红书/百家号等）逻辑为「填草稿 + 人工点发布」，已对照开源选择器加固，但本机未做已登录真机闭环。
- `desktop-shell/` 是封装骨架，不是最终安装包。

## 9. GitHub 使用

当前 GitHub 仓库：

```bash
https://github.com/zd19831227/panboss-jiyou-geo
```

提交代码：

```bash
git status
git add .
git commit -m "说明本次修改"
git push
```

不要提交：

- `backend/.env`
- `backend/geo_data.db`
- `frontend/node_modules/`
- `backend/.venv/`
- `output/`
- `.playwright-cli/`

## 10. 数据备份

真实业务数据在：

```bash
backend/geo_data.db
```

手动备份：

```bash
cp backend/geo_data.db "backend/geo_data.backup_$(date +%Y%m%d_%H%M%S).db"
```

恢复备份前先停止服务：

```bash
python3 start_services.py stop
cp backend/geo_data.backup_YYYYMMDD_HHMMSS.db backend/geo_data.db
python3 start_services.py
```

## 11. 常见问题

### 后端状态离线

```bash
python3 start_services.py status
python3 start_services.py
```

### 设置页打不开

确认前端后端都在：

```bash
python3 start_services.py status
```

然后访问：

```bash
http://localhost:15173/settings
```

### AI 监测没有结果

检查：

1. 系统设置里平台是否可用。
2. 是否运行过平台自检。
3. 是否有验证码。
4. 任务是否失败并记录失败原因。
5. `scripts/restart_recovery_check.py` 是否 ready。

### 引用没有链接

不能算已验证引用，只能算未验证引用。

### 发布任务不能创建

检查：

1. 内容是否已审核。
2. 平台工作流是否 tested/active。
3. 是否绑定账号。
4. 是否配置固定发布 URL。

### 复制到另一台电脑不能写入移动硬盘

如果移动硬盘是 `NTFS`，macOS 默认多数情况下只能读不能写。要正常复制，换 `exFAT`、使用支持写入 NTFS 的驱动，或先复制到本机再转存。

### Windows 能不能直接商用

可以按「半自动运营工具」交付使用：能启动、打开 `15173/18770`、按钮连真实 API、AI 监测可用、发布到草稿后人工确认。  
全平台「已登录账号真机发布闭环」与 `production_gate` 品牌引用样本补齐前，不要对外宣称「无人值守全自动商用」。详见 `usage_test/交付缺口与RPA对照说明_20260724.md`。

## 12. 第三方审计

给另一个 agent 使用：

```bash
THIRD_PARTY_AUDIT_HANDOFF.md
```

审计必须基于：

- 命令输出
- 数据库查询
- 页面实测
- 截图/HTML 文件
- GitHub 仓库内容

不能只相信文字说明。
