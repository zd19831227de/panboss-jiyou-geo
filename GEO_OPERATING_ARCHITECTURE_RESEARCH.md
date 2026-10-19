# GEO 运营知识库与平台图谱调研评估

更新时间：2026-07-08

## 结论

GEO 下一阶段不应先做“多加几个发布平台”，而应先升级为“品牌知识库 + 行业信源图谱 + 平台选择引擎 + 引用监控诊断”的运营系统。

发布平台只是分发层。平台是否进入可执行项，必须同时满足行业相关、内容类型匹配、账号可用、工作流 tested/active、有固定发布 URL、有测试证据、能回捞和验真 URL。未测试平台只能进入待接入清单，不能创建真实发布任务。

房产品牌策略中，携程旅拍不应作为可执行发布项。安居客、房天下、贝壳、乐居等房产垂直平台可进入行业信源图谱和待接入规划，但在完成账号、发布页、RPA、URL 回捞、验真证据前，不能标记为可发布能力。

## 调研依据

- Google AI 搜索仍沿用基础 SEO 原则：页面需可抓取、可索引、内容文本可见、结构化数据与页面内容一致；没有特殊 AI schema 或神秘 AI 文件要求。来源：[Google AI features](https://developers.google.com/search/docs/appearance/ai-features)
- Google AI Overviews / AI Mode 会跨子主题和数据源做 query fan-out，因此 GEO 运营要覆盖问题矩阵、实体事实和多信源，不是单篇软文发布。来源：[Google AI features](https://developers.google.com/search/docs/appearance/ai-features)
- Google Helpful Content 要求内容有原创信息、完整描述、清晰来源、专业性、可信度和真实经验；因此知识库必须先沉淀可核验事实和证据，不能用批量软文替代运营资产。来源：[Google Helpful Content](https://developers.google.com/search/docs/fundamentals/creating-helpful-content)
- 结构化数据的作用是帮助搜索系统理解页面实体和内容分类，但不能用不可见或不准确的数据冒充事实。来源：[Google structured data](https://developers.google.com/search/docs/appearance/structured-data/intro-structured-data)
- OpenAI 搜索侧由 OAI-SearchBot 影响 ChatGPT Search 展示，GPTBot 与训练抓取是另一类控制；因此官网、落地页、robots、索引状态必须进入 GEO 运维范围。来源：[OpenAI crawlers](https://platform.openai.com/docs/bots)
- GEO 论文提出生成式引擎会综合多源回答，内容可见性优化有明显行业差异，不能一套平台覆盖所有行业。来源：[GEO: Generative Engine Optimization](https://arxiv.org/abs/2311.09735)
- 2026 年 citation visibility 研究显示，生成式引擎引用更受文档级结构、内容属性和语言特征影响，单纯关键词或表层改写不够；因此系统要做知识库、证据源、结构化问题和引用失败归因。来源：[Think Before Writing: Feature-Level Multi-Objective Optimization for Generative Citation Visibility](https://arxiv.org/abs/2604.19113)

## 成熟 GEO 运营架构

### 1. 品牌知识库

知识库是第一资产。每个品牌至少沉淀这些可审计事实：

- 品牌实体：名称、别名、官网、主体公司、地域、行业、业务范围。
- 产品/项目：项目名称、位置、价格/权益、户型/房型/课程/服务包、适合人群。
- 可信证据：官网 URL、平台页 URL、地图/百科/媒体/监管/资质页、发布时间、截图或摘要。
- 内容边界：禁用话术、不可承诺内容、竞品边界、合规风险。
- FAQ：推荐型、对比型、避坑型、交易型、地域型、政策型、售前问答型。
- 更新状态：来源可信度、最近验证时间、失效时间、负责人、引用次数。

没有证据的事实不能进入内容生产；证据过期或不可访问时必须标记为待复核。

### 2. 问题矩阵

GEO 不是只做品牌词。系统应按行业生成问题矩阵：

- 推荐型：某区域/某预算/某人群怎么选。
- 对比型：品牌 A 和竞品 B 差异。
- 避坑型：购买/入住/报名/到店前注意事项。
- 交易型：价格、优惠、预约、地址、交通、交付周期。
- 口碑型：真实评价、投诉、优缺点、适合和不适合人群。
- 政策型：房产政策、教育资质、医美合规、本地生活许可。

每个问题必须绑定知识库事实、目标信源、目标平台、目标监控平台。

### 3. 行业信源图谱

平台分三类：可信信源、内容分发平台、可执行发布平台。三者不能混用。

房产：

- 可信信源：官网/项目页、地图、百科、政府/住建公开信息、开发商主体信息、媒体报道。
- 垂直信源：安居客、房天下、贝壳、乐居、地方房产媒体。
- 分发平台：知乎、百家号、搜狐号、今日头条、小红书、抖音。
- 当前策略：携程旅拍排除；房产垂直平台先入图谱和待接入，不直接建发布任务。

酒店/本地生活：

- 可信信源：官网、地图、高德/百度地图、携程、美团/大众点评、Trip/Booking/Agoda、地方文旅信息。
- 分发平台：携程旅拍、小红书、抖音、知乎、头条、搜狐、百家号。
- 当前策略：携程可保留，但必须说明推荐原因和账号/工作流门禁。

教育：

- 可信信源：官网、教育主管部门信息、资质证照、课程页、公开评价、媒体报道。
- 分发平台：知乎、百家号、搜狐、头条、小红书、抖音。
- 当前策略：内容必须强化资质、课程、适合人群和合规边界。

医美/本地服务：

- 可信信源：官网、卫健/市场监管信息、资质证照、医生/机构资质、地图、本地评价。
- 分发平台：知乎、小红书、抖音、百家号、搜狐、头条。
- 当前策略：高合规风险行业，发布前必须做禁用话术和证据检查。

## 当前项目评估

### 已有基础

- 已有策略表和执行项：`geo_strategies`、`geo_strategy_actions`。
- 已有内容生产、发布任务、发布工作流、账号绑定、工作流测试、URL 回捞、证据三件套。
- 发布任务已有硬门禁：非官网平台必须 workflow tested/active、固定发布 URL、verified test evidence、绑定账号。
- 监测侧已有 `verified_citation`、`unverified_citation`、`not_cited`，并新增业务质量字段 `business_status`、`effective_source_count`、`brand_mention_rate`。

### 关键缺口

- 缺真正的 GEO 知识库表：当前更多是品牌字段、内容、发布任务，没有事实/证据/问题/信源的可复用运营层。
- 缺行业信源图谱：平台选择仍主要来自固定生态和固定平台池，不能解释“为什么房产排除携程、为什么酒店保留携程”。
- 策略补齐仍偏固定：`backend/app/routers/operations.py` 的 `_ensure_multiplatform_strategy_actions` 固定扫 `website/ctrip_travel/baijiahao/zhihu/douyin/toutiao/sohu/xiaohongshu`，并只给 `website/ctrip_travel/baijiahao/zhihu` 自动补内容/发布动作。
- 默认发布工作流仍偏平台清单：`backend/app/database.py` 初始化固定写入官网、携程、抖音、头条、百家号、搜狐、知乎、小红书。
- 缺引用失败归因：现在能判定业务失败，但还没有把失败稳定拆成未抓取、未索引、知识缺口、平台错配、内容可信度不足、竞品压制、AI 未引用。

### 当前数据快照

本地 `backend/geo_data.db` 于 2026-07-08 只读查询：

- `publishing_workflows`：website=tested；baijiahao/ctrip_travel/zhihu=draft；douyin/sohu/toutiao/xiaohongshu=template；penguin/wechat/xigua=disabled。
- `publishing_tasks`：知乎、携程旅拍、今日头条、搜狐号、小红书、百家号各有 published/verified；抖音当前有 failed/unverified；website 仍有 prepared/material_verified，不能算外部发布成功。
- `monitor_answers`：verified_citation=3，unverified_citation=183，not_cited=481，failed=1121。
- `monitor_runs`：已有 3 条 failed_quality_gate，说明系统开始区分“技术完成”和“业务质量失败”。

## 目标数据模型

第一阶段新增或规划这些表，不破坏现有表：

- `geo_knowledge_items`：品牌事实、事实类型、正文、行业、地域、可信等级、状态、更新时间。
- `geo_evidence_sources`：证据 URL、证据类型、来源平台、截图/HTML/摘要、可访问性、验真状态。
- `geo_question_matrix`：行业问题、意图类型、目标人群、关联知识项、目标监控平台。
- `geo_source_map`：行业、信源类型、平台、优先级、适用内容类型、是否可发布、是否可监控。
- `geo_platform_recommendations`：策略级平台推荐结果、推荐原因、排除原因、执行层级、门禁状态。
- `geo_citation_diagnostics`：监测失败归因、证据缺口、下一步修复动作、复测状态。

项目级知识库资源固定放在仓库内 `geo/` 目录：

- `geo/knowledge_base/`：品牌事实、证据源、问题矩阵、引用诊断等知识库资源。
- `geo/source_maps/`：行业信源图谱和平台选择规则。

后续封装、迁移、交付必须包含整个 `geo/` 目录。缺少 `geo/` 时，只能算代码包，不能算完整 GEO 项目包。

执行层级建议统一为：

- `source_only`：只做信源，不可发布。
- `candidate`：候选平台，未接入。
- `draft_ready`：可准备草稿，不能标记发布成功。
- `workflow_tested`：工作流测试通过，可创建任务。
- `verified_loop`：发布、URL 回捞、验真、三件套、复测闭环完整。

## 实施路线

### 第一阶段：文档和模型

产物就是本文档。成功标准：

- 明确 GEO 运营架构。
- 明确行业平台图谱。
- 明确现有系统缺口。
- 明确下一阶段数据库、接口、前端、门禁落点。

### 第二阶段：知识库与信源图谱

后端新增知识库和信源图谱表，并从 `geo/` 加载项目级知识库资源。首版只支持 CRUD、只读推荐和门禁检查，不自动执行外部发布。

接口建议：

- `GET/POST /api/operations/knowledge/items`
- `GET/POST /api/operations/knowledge/evidence`
- `GET/POST /api/operations/source-map`
- `POST /api/operations/source-map/recommend-platforms`

2026-07-08 已完成首个可调用底座：

- `backend/app/services/publish_platform_selection.py`：从项目内 `geo/source_maps/industry_source_map.seed.json` 读取行业信源图谱并输出推荐、排除、待接入平台。
- `POST /api/operations/publish/platforms/recommend`：返回平台推荐规划，不创建发布任务。
- `scripts/platform_selection_gate.py`：验证项目内 `geo/` 资源存在、知识库必须随项目封装、房产品牌排除携程旅拍、安居客/房天下/贝壳/乐居只作为待接入。
- `frontend/src/views/Operations.vue`：AI 策略中枢展示推荐平台、排除平台、待接入平台。
- `backend/app/services/geo_knowledge_base.py`：从项目内 `geo/knowledge_base/` 读写品牌事实、证据源、问题矩阵、引用诊断 JSON 资源。
- `GET /api/operations/knowledge`、`GET/POST /api/operations/knowledge/{resource}`：提供项目内知识库摘要和资源读写。
- `scripts/knowledge_base_gate.py`：验证知识库资源随项目存在、可读写、接口层可调用，并拒绝 secret-like 字段。
- `strategy/generate` 已接入 `geo/knowledge_base/`：策略上下文会读取品牌事实、证据源、问题矩阵、引用诊断，并强制输出 `knowledge_references` 和 `evidence_gaps`。
- `scripts/strategy_knowledge_gate.py`：验证策略上下文能读取项目内知识库、缺证据事实会进入证据缺口，不会被当成可发布事实。
- `frontend/src/views/Operations.vue`：新增“GEO知识库”运营管理区，可查看和新增品牌事实、证据源、问题矩阵、引用诊断四类资源；写入动作调用真实 `POST /api/operations/knowledge/{resource}`，并在界面标明 `geo/` 必须随项目封装。
- `backend/app/services/geo_knowledge_base.py`：文件型知识库写入增加资源级文件锁和唯一临时文件，避免并发门禁或多 agent 同时写入时覆盖记录。
- `backend/app/database.py`：补齐旧库 `publishing_workflow_tests.updated_at` 迁移，避免工作流测试记录接口在旧 DB 上返回 500。
- `scripts/ui_contract_gate.py`：扩展 UI 契约门禁，强制检查知识库入口、四类资源、读写接口、写入按钮和项目内封装提示。

### 第三阶段：平台选择服务

新增 `backend/app/services/publish_platform_selection.py`。

输入：

- brand.industry、brand.products、brand.target_audience、brand.objective。
- 知识库完整度。
- 行业信源图谱。
- publishing_workflows 状态。
- publishing_accounts 状态。
- 最近监控结果和 citation diagnostics。

输出：

- `recommended_publish_platforms`
- `excluded_publish_platforms`
- `publishing_workflow_todos`
- `source_platforms`
- `execution_gate`

房产规则首版：

- 排除 `ctrip_travel` 可执行项。
- 保留 `website/zhihu/baijiahao/toutiao/sohu/xiaohongshu/douyin` 作为内容分发候选，但只有满足工作流门禁才可执行。
- 新增 `anjuke/fangtianxia/beike/leju` 为 `source_only` 或 `candidate`，默认不可建发布任务。

### 第四阶段：策略中枢改造

替换 `_ensure_multiplatform_strategy_actions` 的固定平台补齐逻辑。

新逻辑：

- 先调用平台选择服务。
- 只为 `workflow_tested/verified_loop` 平台生成 `create_publish_task`。
- 只为 `draft_ready` 平台生成 `prepare_rpa_draft`。
- 对 `candidate/source_only` 只生成待接入说明，不生成可执行发布任务。
- 策略 JSON 写入推荐原因、排除原因、门禁状态。

### 第五阶段：前端运营中台

`frontend/src/views/Operations.vue` 的 AI 策略右侧执行项需要展示：

- 推荐平台。
- 推荐原因。
- 排除平台。
- 排除原因。
- 当前执行层级。
- 工作流门禁。
- 待接入动作。

不可执行项必须显示原因，按钮禁用，不能让用户误以为“后台配置过平台就能发布”。

### 第六阶段：门禁

新增或扩展：

- `scripts/platform_selection_gate.py`：房产策略不出现携程旅拍可执行项；房产垂直平台默认不可建任务。
- `scripts/knowledge_base_gate.py`：项目内知识库资源必须存在、可调用、可随包迁移，且不得写入 token/cookie/password 等敏感字段。
- `scripts/strategy_knowledge_gate.py`：策略生成链路必须读取项目内知识库，并把缺证据事实写入 `evidence_gaps/gap_analysis`。
- `scripts/p3_strategy_gate.py`：策略必须包含推荐/排除/待接入结构。
- `scripts/ui_contract_gate.py`：前端必须展示门禁状态、禁用原因、GEO 知识库管理入口和真实写入动作。
- `scripts/production_gate.py`：继续禁止未验真平台进入商业闭环。

## 下一阶段成功标准

- 房产品牌生成策略时，`ctrip_travel` 不出现在可执行发布项。
- 酒店/本地生活品牌仍可推荐 `ctrip_travel`，但必须有推荐原因。
- 安居客、房天下、贝壳、乐居只显示为候选/信源/待接入，不允许创建发布任务。
- 已测试平台仍走现有发布任务门禁，不降低 URL 验真和三件套要求。
- 每条策略都能解释：为什么推荐、为什么排除、为什么不可执行、下一步接入什么。

## 禁止事项

- 禁止把未测试平台写成可执行能力。
- 禁止绕过 `publishing_workflows` tested/active 和 verified test evidence。
- 禁止把平台配置存在后台等同于发布闭环。
- 禁止为了房产平台短期跑通而放松 URL 回捞、验真、截图/HTML/摘要三件套。
- 禁止把 AI 未引用品牌包装成 GEO 成功。
