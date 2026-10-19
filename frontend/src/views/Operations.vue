<template>
  <div class="ops">
    <div class="ops-head">
      <el-select v-model="brandId" placeholder="选择品牌" style="width:260px" @change="changeBrand">
        <el-option v-for="brand in brands" :key="brand.id" :label="brand.name" :value="brand.id" />
      </el-select>
      <el-button type="primary" @click="generateIdeas" :disabled="!brandId || busy.generateIdeas" :loading="busy.generateIdeas">生成关键词</el-button>
      <el-button @click="refreshAll" :loading="busy.refreshAll">刷新</el-button>
    </div>

    <div class="workbench">
      <div class="metric">
        <span>待采纳词</span>
        <b>{{ workbench.keyword_status?.pending || 0 }}</b>
      </div>
      <div class="metric">
        <span>待审核内容</span>
        <b>{{ workbench.content_status?.draft || 0 }}</b>
      </div>
      <div class="metric">
        <span>待发布</span>
        <b>{{ workbench.publish_status?.pending || 0 }}</b>
      </div>
      <div class="metric">
        <span>待回填链接</span>
        <b>{{ workbench.publish_status?.prepared || 0 }}</b>
      </div>
      <div class="actions">
        <el-tag v-for="item in workbench.next_actions" :key="item" size="small">{{ item }}</el-tag>
      </div>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane label="AI策略中枢" name="strategy">
        <div class="toolbar">
          <el-input
            v-model="strategyObjective"
            placeholder="可选：填写本轮 GEO 目标，例如提升市中心酒店推荐场景"
            style="width:420px"
          />
          <el-button type="primary" @click="generateStrategy" :disabled="!brandId || busy.generateStrategy" :loading="busy.generateStrategy">AI生成策略</el-button>
          <el-button type="success" @click="confirmStrategyActions" :disabled="!activeStrategy || !selectedStrategyActions.length || busy.confirmStrategy" :loading="busy.confirmStrategy">确认选中执行项</el-button>
          <el-button type="warning" @click="executeStrategyActions" :disabled="!canExecuteStrategyActions || executingStrategy" :loading="executingStrategy">执行已确认项</el-button>
          <el-button @click="refreshStrategies" :disabled="!brandId">刷新策略</el-button>
          <span class="muted">P4 只执行白名单 API/脚本；外部发布仍需人工确认</span>
          <span v-if="activeStrategy && !canExecuteStrategyActions" class="warning-hint">先勾选执行项并点击确认</span>
        </div>
        <div v-if="activeStrategy" class="strategy-layout">
          <section class="strategy-main">
            <div class="strategy-title">
              <div>
                <h3>品牌 GEO 策略</h3>
                <p>{{ activeStrategy.strategy?.positioning || '暂无定位摘要' }}</p>
              </div>
              <el-tag :type="strategyStatusType(activeStrategy.status)">{{ strategyStatusLabel(activeStrategy.status) }}</el-tag>
            </div>
            <div class="strategy-grid">
              <div class="strategy-block">
                <b>搜索意图</b>
                <span>{{ strategySectionItems(activeStrategy.strategy?.search_intent).join('；') || '暂无' }}</span>
              </div>
              <div class="strategy-block">
                <b>监测计划</b>
                <span>{{ strategySectionItems(activeStrategy.strategy?.monitoring_plan).join('；') || '暂无' }}</span>
              </div>
              <div class="strategy-block">
                <b>内容计划</b>
                <span>{{ strategySectionItems(activeStrategy.strategy?.content_plan).join('；') || '暂无' }}</span>
              </div>
              <div class="strategy-block">
                <b>发布计划</b>
                <span>{{ strategySectionItems(activeStrategy.strategy?.publishing_plan).join('；') || '暂无' }}</span>
              </div>
              <div class="strategy-block">
                <b>复测周期</b>
                <span>{{ activeStrategy.strategy?.retest_cycle || '暂无' }}</span>
              </div>
              <div class="strategy-block">
                <b>成功标准</b>
                <span>{{ strategySectionItems(activeStrategy.strategy?.success_criteria).join('；') || '暂无' }}</span>
              </div>
            </div>
            <div class="gap-box">
              <b>缺口分析</b>
              <el-tag v-for="item in strategySectionItems(activeStrategy.strategy?.gap_analysis)" :key="item" size="small" type="warning">{{ item }}</el-tag>
            </div>
            <div class="gap-box">
              <b>发布能力分层</b>
              <el-tag v-for="item in strategySectionItems(activeStrategy.strategy?.publishing_capability_plan)" :key="item" size="small" type="info">{{ item }}</el-tag>
            </div>
            <div class="gap-box">
              <b>推荐发布平台</b>
              <el-tag
                v-for="item in activeStrategy.strategy?.recommended_publish_platforms || []"
                :key="item.platform"
                size="small"
                :type="platformDecisionTagType(item.execution_level)"
              >
                {{ item.platform_name || platformLabel(item.platform) }}：{{ executionLevelLabel(item.execution_level) }}
              </el-tag>
            </div>
            <div class="gap-box">
              <b>排除平台</b>
              <el-tag
                v-for="item in activeStrategy.strategy?.excluded_publish_platforms || []"
                :key="item.platform"
                size="small"
                type="danger"
              >
                {{ item.platform_name || platformLabel(item.platform) }}：{{ item.reason }}
              </el-tag>
            </div>
            <div class="gap-box">
              <b>待接入平台</b>
              <el-tag
                v-for="item in activeStrategy.strategy?.publishing_workflow_todos || []"
                :key="item.platform"
                size="small"
                type="warning"
              >
                {{ item.platform_name || platformLabel(item.platform) }}：{{ executionLevelLabel(item.execution_level || 'candidate') }}
              </el-tag>
            </div>
            <div class="strategy-meta">
              模型：{{ activeStrategy.llm_model || '未知' }} / 用量编号：{{ activeStrategy.llm_usage_id || '-' }} / 数据：
              关键词 {{ activeStrategy.strategy?.real_data_summary?.keywords || 0 }}，
              监测 {{ activeStrategy.strategy?.real_data_summary?.monitor_results || 0 }}，
              内容 {{ activeStrategy.strategy?.real_data_summary?.content_assets || 0 }}，
              发布 {{ activeStrategy.strategy?.real_data_summary?.publish_tasks || 0 }}
            </div>
          </section>
          <section class="strategy-actions">
            <div class="sub-toolbar">
              <span>可选执行项：{{ activeStrategy.actions?.length || 0 }}</span>
              <span>已确认：{{ strategyActionStats.confirmed }}</span>
              <span>已执行：{{ strategyActionStats.executed }}</span>
              <span>待处理：{{ strategyActionStats.blocked }}</span>
              <span>失败：{{ strategyActionStats.failed }}</span>
              <span>待确认：{{ strategyActionStats.pending }}</span>
              <span class="muted">已确认后才允许执行</span>
            </div>
            <div v-if="strategyExecutionSummary || strategyFailedActions.length" class="execution-summary">
              <div class="execution-title">
                <b>执行结果摘要</b>
                <span v-if="strategyExecutionSummary">
                  成功 {{ strategyExecutionSummary.executed_count }}，待处理 {{ strategyExecutionSummary.blocked_count || 0 }}，失败 {{ strategyExecutionSummary.failed_count }}
                </span>
                <span v-else>
                  当前待处理 {{ strategyBlockedActions.length }} 项，失败 {{ strategyFailedActions.length }} 项
                </span>
              </div>
              <div v-if="strategyBlockedActions.length" class="failure-list">
                <div v-for="item in strategyBlockedActions" :key="item.id" class="failure-item">
                  <b>{{ item.title || actionTypeText(item.action_type) }}</b>
                  <span>{{ item.failure_reason || '缺少前置条件，等待人工处理' }}</span>
                </div>
              </div>
              <div v-if="strategyFailedActions.length" class="failure-list">
                <div v-for="item in strategyFailedActions" :key="item.id" class="failure-item">
                  <b>{{ item.title || actionTypeText(item.action_type) }}</b>
                  <span>{{ item.failure_reason || '执行失败，需查看后端结果' }}</span>
                </div>
              </div>
              <div class="next-step">
                下一步：先到“内容生产”审核通过 AI 草稿，再创建发布任务；发布完成并回填真实链接后，才能做自动填草稿和发布后复测。
              </div>
            </div>
            <el-table :data="activeStrategy.actions || []" @selection-change="selectedStrategyActions = $event" stripe>
              <el-table-column type="selection" width="45" />
              <el-table-column prop="title" label="执行项" min-width="220" show-overflow-tooltip />
              <el-table-column label="类型" width="130">
                <template #default="{ row }">{{ actionTypeText(row.action_type) }}</template>
              </el-table-column>
              <el-table-column label="风险" width="80">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.cost_risk === 'high' ? 'danger' : row.cost_risk === 'medium' ? 'warning' : 'success'">{{ riskLabel(row.cost_risk) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="登录" width="80">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.requires_login ? 'warning' : 'info'">{{ row.requires_login ? '需要' : '不需要' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="100">
                <template #default="{ row }">{{ actionStatusLabel(row.status) }}</template>
              </el-table-column>
              <el-table-column label="执行结果" width="120">
                <template #default="{ row }">
                  <el-tag v-if="row.status === 'executed'" size="small" type="success">已执行</el-tag>
                  <el-tag v-else-if="row.status === 'blocked'" size="small" type="warning">待处理</el-tag>
                  <el-tag v-else-if="row.status === 'failed'" size="small" type="danger">失败</el-tag>
                  <span v-else class="muted">未执行</span>
                </template>
              </el-table-column>
              <el-table-column prop="failure_reason" label="失败原因" min-width="180" show-overflow-tooltip />
              <el-table-column prop="description" label="说明" min-width="260" show-overflow-tooltip />
            </el-table>
          </section>
        </div>
        <el-empty v-else description="选择品牌后生成第一版 AI GEO 策略" />
      </el-tab-pane>

      <el-tab-pane label="GEO知识库" name="knowledge">
        <div class="toolbar">
          <el-radio-group v-model="knowledgeResource" @change="refreshKnowledgeRecords">
            <el-radio-button
              v-for="item in knowledgeResourceOptions"
              :key="item.value"
              :value="item.value"
            >
              {{ item.label }}
            </el-radio-button>
          </el-radio-group>
          <el-button type="primary" @click="refreshKnowledge" :loading="busy.refreshKnowledge">刷新知识库</el-button>
          <span class="muted">项目资源：{{ knowledgeSummary.root || 'geo/knowledge_base' }}；封装必须包含 geo/</span>
        </div>

        <div class="knowledge-layout">
          <section class="knowledge-panel">
            <div class="knowledge-title">
              <div>
                <h3>{{ activeKnowledgeResource.label }}</h3>
                <p>{{ activeKnowledgeResource.hint }}</p>
              </div>
              <el-tag :type="knowledgeSummary.packaging_required ? 'success' : 'danger'">
                {{ knowledgeSummary.packaging_required ? '随项目封装' : '封装缺失' }}
              </el-tag>
            </div>
            <div class="knowledge-stats">
              <div
                v-for="item in knowledgeResourceOptions"
                :key="item.value"
                class="knowledge-stat"
              >
                <span>{{ item.label }}</span>
                <b>{{ knowledgeSummary.resources?.[item.value]?.count || 0 }}</b>
                <small>{{ knowledgeSummary.resources?.[item.value]?.path || '-' }}</small>
              </div>
            </div>
            <el-table :data="knowledgeRecords" stripe>
              <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
              <el-table-column label="分类" width="120" show-overflow-tooltip>
                <template #default="{ row }">{{ row.category || '-' }}</template>
              </el-table-column>
              <el-table-column prop="status" label="状态" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'">{{ knowledgeStatusLabel(row.status || 'active') }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="证据/问题/平台" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ row.source_url || row.question || row.platform || row.evidence_type || row.intent_type || '-' }}
                </template>
              </el-table-column>
              <el-table-column prop="content" label="内容" min-width="300" show-overflow-tooltip />
              <el-table-column prop="updated_at" label="更新时间" width="170" />
            </el-table>
          </section>

          <section class="knowledge-panel">
            <div class="knowledge-title">
              <div>
                <h3>新增知识记录</h3>
                <p>只写项目内 JSON 知识库；敏感字段由后端拒绝。</p>
              </div>
            </div>
            <el-form label-width="90px">
              <el-form-item label="标题">
                <el-input v-model="knowledgeForm.title" placeholder="例如：品牌核心卖点 / 官方证据源 / 引用失败原因" />
              </el-form-item>
              <el-form-item label="分类">
                <el-input v-model="knowledgeForm.category" placeholder="事实类型、信源类型、问题类型或诊断类型" />
              </el-form-item>
              <el-form-item label="正文">
                <el-input v-model="knowledgeForm.content" type="textarea" :rows="5" placeholder="填写可审计事实、证据说明、结构化问题或诊断结论" />
              </el-form-item>
              <el-form-item label="来源URL">
                <el-input v-model="knowledgeForm.source_url" placeholder="可选：必须是真实可追溯页面" />
              </el-form-item>
              <el-form-item label="问题">
                <el-input v-model="knowledgeForm.question" placeholder="问题矩阵可填；其他资源可留空" />
              </el-form-item>
              <el-form-item label="平台">
                <el-input v-model="knowledgeForm.platform" placeholder="例如：网页搜索 / 千问 / 元宝 / 行业信源平台" />
              </el-form-item>
              <el-form-item label="状态">
                <el-select v-model="knowledgeForm.status" style="width:160px">
                  <el-option label="有效" value="active" />
                  <el-option label="待补证据" value="evidence_gap" />
                  <el-option label="停用" value="disabled" />
                </el-select>
              </el-form-item>
            </el-form>
            <div class="knowledge-actions">
              <el-button type="primary" @click="saveKnowledgeRecord" :disabled="!canSaveKnowledge" :loading="busy.saveKnowledge">写入知识库</el-button>
              <el-button @click="resetKnowledgeForm">清空</el-button>
              <span class="muted">资源：{{ activeKnowledgeResource.file }}</span>
            </div>
          </section>
        </div>
      </el-tab-pane>

      <el-tab-pane label="来源生态" name="ecosystems">
        <div class="toolbar">
          <el-button type="primary" @click="refreshEcosystems" :disabled="!brandId || busy.refreshEcosystems" :loading="busy.refreshEcosystems">刷新来源生态</el-button>
        </div>
        <el-table :data="ecosystems" stripe>
          <el-table-column prop="ecosystem_name" label="生态" width="130" />
          <el-table-column label="AI平台" width="140">
            <template #default="{ row }">
              <el-tag v-for="platform in row.ai_platforms" :key="platform" size="small" style="margin-right:4px">{{ platformLabel(platform) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="evidence_count" label="来源证据数" width="110" />
          <el-table-column label="命中来源域名" min-width="260">
            <template #default="{ row }">
              <span v-if="!row.source_hits?.length" class="muted">暂无真实来源命中</span>
              <el-tag v-for="source in row.source_hits" :key="source.platform + source.domain" size="small" style="margin-right:4px">
                {{ platformLabel(source.platform) }} · {{ source.domain }} ×{{ source.count }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="推荐发布入口" min-width="260">
            <template #default="{ row }">
              <el-tag v-for="item in row.recommendations" :key="item.platform" size="small" :type="item.status === 'tested' || item.status === 'active' ? 'success' : 'warning'" style="margin-right:4px">
                {{ item.platform_name }} / {{ workflowStatusText(item.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="basis" label="依据" min-width="320" show-overflow-tooltip />
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="发布平台规划" name="workflows">
        <div class="toolbar">
          <el-button type="primary" @click="refreshWorkflows" :loading="busy.refreshWorkflows">刷新规划</el-button>
        </div>
        <el-table :data="workflows" stripe>
          <el-table-column prop="platform_name" label="平台" width="120" />
          <el-table-column prop="login_url" label="登录链接" min-width="220" show-overflow-tooltip />
          <el-table-column prop="publish_url" label="固定发布链接" min-width="260" show-overflow-tooltip />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="workflowStatusType(row.status)">{{ workflowStatusText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="last_tested_at" label="最后测试" width="170" />
          <el-table-column label="选择器" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="workflowSelectorReady(row) ? 'success' : 'warning'">
                {{ workflowSelectorReady(row) ? '已固化' : (row.platform === 'website' ? '不需要' : '缺失') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="闭环能力" width="130">
            <template #default="{ row }">
              <el-tag size="small" :type="capabilityType(row.capability?.level)">
                {{ capabilityText(row.capability?.level) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="notes" label="说明" min-width="260" show-overflow-tooltip />
          <el-table-column label="操作" width="430">
            <template #default="{ row }">
              <el-button size="small" type="primary" @click="editWorkflow(row)">编辑</el-button>
              <el-button size="small" @click="loadWorkflowTests(row)">记录</el-button>
              <el-button size="small" @click="openWorkflowLogin(row)" :disabled="row.platform === 'website'">登录</el-button>
              <el-button size="small" @click="openWorkflowPublish(row)" :disabled="row.platform === 'website'">发布页</el-button>
              <el-button size="small" :loading="discoveringWorkflow === row.platform" @click="discoverWorkflowSelectors(row)" :disabled="row.platform === 'website'">识别</el-button>
              <el-button size="small" :loading="testingWorkflow === row.platform" :disabled="!canTestWorkflow(row)" @click="testWorkflow(row)">测试</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="sub-toolbar">
          <span>测试记录：{{ platformLabel(selectedWorkflowPlatform) }}</span>
          <el-button size="small" @click="loadWorkflowTestsByPlatform(selectedWorkflowPlatform)" :disabled="!selectedWorkflowPlatform">刷新记录</el-button>
        </div>
        <el-table :data="workflowTests" stripe style="margin-top:14px">
          <el-table-column label="平台" width="100">
            <template #default="{ row }">{{ platformLabel(row.platform) }}</template>
          </el-table-column>
          <el-table-column label="测试状态" width="140">
            <template #default="{ row }">{{ publishStatusLabel(row.status) }}</template>
          </el-table-column>
          <el-table-column prop="tested_url" label="测试链接" min-width="240" show-overflow-tooltip />
          <el-table-column label="证据" width="150">
            <template #default="{ row }">
              <a v-if="row.raw_artifact_verified && row.raw_artifact_path" :href="artifactUrl(row.raw_artifact_path)" target="_blank">网页原文</a>
              <span v-if="row.raw_artifact_verified && row.raw_artifact_path && row.screenshot_verified && row.screenshot_path"> / </span>
              <a v-if="row.screenshot_verified && row.screenshot_path" :href="artifactUrl(row.screenshot_path)" target="_blank">截图</a>
            </template>
          </el-table-column>
          <el-table-column prop="failure_reason" label="失败/待处理原因" min-width="220" show-overflow-tooltip />
          <el-table-column prop="created_at" label="时间" width="170" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="关键词生产" name="keywords">
        <div class="toolbar">
          <el-input v-model="seed" placeholder="种子词，可选" style="width:280px" />
          <el-input-number v-model="ideaCount" :min="5" :max="100" />
          <el-button type="primary" @click="generateIdeas" :disabled="!brandId || busy.generateIdeas" :loading="busy.generateIdeas">AI生成</el-button>
          <el-button type="success" @click="adoptSelected" :disabled="!selectedIdeas.length || busy.adoptIdeas" :loading="busy.adoptIdeas">采纳为关键词</el-button>
        </div>
        <div class="toolbar">
          <el-select v-model="ideaBatchFilter" placeholder="批次筛选" clearable style="width:320px" @change="refreshIdeas">
            <el-option label="全部批次" value="" />
            <el-option label="最新批次" value="__latest__" />
            <el-option
              v-for="batch in ideaBatches"
              :key="batch.generation_batch"
              :label="`${batch.batch_label}（${batch.total_count}条）`"
              :value="batch.generation_batch"
            />
          </el-select>
          <el-select v-model="ideaValueFilter" placeholder="GEO价值类型" clearable style="width:180px" @change="refreshIdeas">
            <el-option label="泛需求推荐词" value="泛需求推荐词" />
            <el-option label="场景推荐词" value="场景推荐词" />
            <el-option label="品牌认知词" value="品牌认知词" />
            <el-option label="售前问答词" value="售前问答词" />
          </el-select>
          <el-select v-model="ideaBrandFilter" placeholder="是否带品牌" clearable style="width:160px" @change="refreshIdeas">
            <el-option label="不带品牌" value="generic_demand" />
            <el-option label="带品牌" value="brand_included" />
          </el-select>
          <el-tag v-if="latestGeneratedBatch" type="success">最新生成：{{ latestGeneratedBatchLabel }}</el-tag>
        </div>
        <el-table :data="ideas" @selection-change="selectedIdeas = $event" stripe>
          <el-table-column type="selection" width="45" />
          <el-table-column label="关键词" min-width="250">
            <template #default="{ row }">
              <div class="keyword-cell">
                <span>{{ row.keyword }}</span>
                <el-tag v-if="row.generation_batch && row.generation_batch === latestGeneratedBatch" size="small" type="success">新生成</el-tag>
                <el-tag v-if="row.status === 'duplicate' || row.is_duplicate" size="small" type="warning">已存在</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="分类" width="100">
            <template #default="{ row }">{{ categoryLabel(row.category) }}</template>
          </el-table-column>
          <el-table-column prop="intent" label="意图" width="120" />
          <el-table-column prop="score" label="评分" width="80" />
          <el-table-column label="GEO价值" width="130">
            <template #default="{ row }">
              <el-tag size="small" :type="geoValueTagType(row.geo_value_type)">{{ row.geo_value_type || '-' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="品牌" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.brand_mention_type === 'brand_included' ? 'warning' : 'success'">
                {{ row.brand_mention_type === 'brand_included' ? '带品牌' : '不带品牌' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="批次" width="190" show-overflow-tooltip>
            <template #default="{ row }">{{ row.batch_label || row.generation_batch || '-' }}</template>
          </el-table-column>
          <el-table-column label="来源" width="130">
            <template #default="{ row }">
              <el-tag size="small" :type="row.source === 'llm_generated' ? 'success' : 'info'">
                {{ row.source === 'llm_generated' ? (row.llm_model || 'AI生成') : (row.source === 'template' ? '模板' : (row.source || '未知来源')) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">{{ ideaStatusLabel(row.status) }}</template>
          </el-table-column>
          <el-table-column prop="reason" label="依据" min-width="260" show-overflow-tooltip />
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="内容生产" name="content">
        <div class="toolbar">
          <el-select v-model="contentForm.keyword_id" placeholder="选择关键词" style="width:280px" filterable>
            <el-option v-for="kw in keywords" :key="kw.id" :label="kw.word" :value="kw.id" />
          </el-select>
          <el-input v-model="contentKeyword" placeholder="或输入真实用户问题" style="width:280px" />
          <el-select v-model="contentForm.content_type" style="width:140px">
            <el-option label="FAQ问答" value="faq" />
            <el-option label="攻略" value="guide" />
            <el-option label="榜单" value="ranking" />
            <el-option label="测评" value="review" />
            <el-option label="问答稿" value="qa" />
            <el-option label="媒体稿" value="media" />
          </el-select>
          <el-select v-model="contentForm.target_platform" style="width:150px">
            <el-option
              v-for="workflow in activeWorkflows"
              :key="workflow.platform"
              :label="`${workflow.platform_name} · ${workflowStatusText(workflow.status)}`"
              :value="workflow.platform"
            />
          </el-select>
          <el-button type="primary" @click="generateContent" :disabled="!canGenerateContent || busy.generateContent" :loading="busy.generateContent">AI生成待审草稿</el-button>
        </div>
        <div class="filter-bar" style="margin:12px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <el-select v-model="contentFilter.brand_id" placeholder="全部品牌" style="width:180px" clearable filterable @change="refreshContents">
            <el-option v-for="b in brands" :key="b.id" :label="b.name" :value="b.id" />
          </el-select>
          <el-input v-model="contentFilter.keyword" placeholder="搜索标题/关键词" style="width:240px" clearable @clear="refreshContents" @keyup.enter="refreshContents" />
          <el-select v-model="contentFilter.status" placeholder="状态筛选" style="width:130px" clearable @change="refreshContents">
            <el-option label="草稿" value="draft" />
            <el-option label="已审核" value="approved" />
            <el-option label="已排队" value="queued" />
            <el-option label="发布中" value="publishing" />
            <el-option label="已发布" value="published" />
            <el-option label="已拒绝" value="rejected" />
          </el-select>
          <el-select v-model="contentFilter.content_type" placeholder="类型筛选" style="width:130px" clearable @change="refreshContents">
            <el-option label="FAQ问答" value="faq" />
            <el-option label="攻略" value="guide" />
            <el-option label="榜单" value="ranking" />
            <el-option label="测评" value="review" />
            <el-option label="问答稿" value="qa" />
            <el-option label="媒体稿" value="media" />
          </el-select>
          <el-select v-model="contentFilter.target_platform" placeholder="平台筛选" style="width:150px" clearable @change="refreshContents">
            <el-option label="官网/落地页" value="website" />
            <el-option label="携程内容中心/旅拍" value="ctrip_travel" />
            <el-option label="抖音创作者中心" value="douyin" />
            <el-option label="今日头条号" value="toutiao" />
            <el-option label="百家号" value="baijiahao" />
            <el-option label="搜狐号" value="sohu" />
            <el-option label="知乎" value="zhihu" />
            <el-option label="小红书" value="xiaohongshu" />
          </el-select>
          <el-select v-model="contentFilter.source" placeholder="来源筛选" style="width:140px" clearable @change="refreshContents">
            <el-option label="AI生成" value="llm_generated" />
            <el-option label="模板降级" value="template_fallback" />
            <el-option label="最小兜底" value="minimal_fallback" />
          </el-select>
          <el-button type="primary" size="small" @click="refreshContents">搜索</el-button>
          <el-button size="small" @click="resetContentFilter">重置</el-button>
          <span class="muted" style="font-size:12px">共 {{ contents.length }} 条</span>
        </div>
        <div v-if="contentQuality" class="quality-bar">
          <el-tag :type="contentQuality.passed ? 'success' : 'warning'" size="small">
            {{ contentQuality.passed ? '质量检查通过' : '质量检查未通过' }}
          </el-tag>
          <span v-if="contentQuality.issues?.length">{{ contentQuality.issues.join('；') }}</span>
        </div>
        <div v-if="contentQuality?.sections?.length" class="quality-sections">
          <div v-for="section in contentQuality.sections" :key="section.name" class="quality-section">
            <div class="quality-section-head">
              <b>{{ section.name }}</b>
              <el-tag size="small" :type="section.passed ? 'success' : 'warning'">{{ section.passed ? '通过' : '需处理' }}</el-tag>
            </div>
            <p>{{ section.mechanism }}</p>
            <div v-if="section.model" class="muted">模型：{{ section.model }} / 用量编号：{{ section.usage_id || '-' }}</div>
            <ul v-if="section.issues?.length">
              <li v-for="issue in section.issues" :key="issue">{{ issue }}</li>
            </ul>
            <ul v-if="section.suggestions?.length" class="suggestions">
              <li v-for="item in section.suggestions" :key="item">{{ item }}</li>
            </ul>
          </div>
        </div>
        <el-table :data="contents" stripe>
          <el-table-column label="品牌" width="140" show-overflow-tooltip>
            <template #default="{ row }">{{ row.brand_name || '-' }}</template>
          </el-table-column>
          <el-table-column prop="title" label="标题" min-width="260" show-overflow-tooltip />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">{{ contentTypeLabel(row.content_type) }}</template>
          </el-table-column>
          <el-table-column label="平台" width="120">
            <template #default="{ row }">{{ platformLabel(row.target_platform) }}</template>
          </el-table-column>
          <el-table-column prop="quality_score" label="质量分" width="80" />
          <el-table-column label="来源/模型" width="150">
            <template #default="{ row }">
              <el-tag size="small" :type="row.source === 'llm_generated' ? 'success' : row.source === 'template_fallback' ? 'warning' : 'info'">{{ row.source === 'llm_generated' ? 'AI生成' : row.source === 'template_fallback' ? '模板降级' : row.source === 'minimal_fallback' ? '最小兜底' : '未知来源' }}</el-tag>
              <div v-if="row.llm_model" class="muted" style="font-size:11px;margin-top:2px">{{ row.llm_model }}</div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="contentStatusType(row.status)">{{ statusText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="质量" width="120">
            <template #default="{ row }">
              <el-button size="small" @click="checkContentQuality(row)" :loading="busy.checkContentQuality === row.id">检查</el-button>
            </template>
          </el-table-column>
          <el-table-column label="图片/视频" width="170">
            <template #default="{ row }">
              <el-button size="small" @click="selectMedia(row, false)">选文件</el-button>
              <el-button size="small" @click="selectMedia(row, true)">文件夹</el-button>
              <el-button size="small" type="danger" @click="clearMedia(row)" :disabled="!mediaCount(row)">清空</el-button>
              <span class="media-count">{{ mediaCount(row) }} 个</span>
            </template>
          </el-table-column>
          <el-table-column label="发布门禁" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <el-tag v-if="canCreatePublish(row)" size="small" type="success">可建任务</el-tag>
              <el-tag v-else size="small" type="warning">{{ publishGateReason(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="620" fixed="right">
            <template #default="{ row }">
              <div class="table-actions">
                <el-button size="small" @click="previewContent(row)">预览</el-button>
                <el-button size="small" type="primary" @click="editContent(row)">编辑</el-button>
                <el-button size="small" type="primary" plain @click="rewriteContent(row, 'rewrite')" :loading="busy.rewriteContent === row.id">AI重写</el-button>
                <el-button size="small" type="warning" plain @click="rewriteContent(row, 'fix_gate')" :loading="busy.rewriteContent === row.id">门禁修复</el-button>
                <el-button size="small" plain @click="rewriteContent(row, 'regenerate')" :loading="busy.rewriteContent === row.id">重新生成</el-button>
                <el-button size="small" type="warning" @click="approveContent(row)" :disabled="row.status === 'published'" :loading="busy.approveContent === row.id">审核通过</el-button>
                <el-button size="small" type="success" @click="createPublishTask(row)" :disabled="!canCreatePublish(row) || busy.createPublishTask === row.id" :loading="busy.createPublishTask === row.id">建发布任务</el-button>
                <el-button size="small" type="danger" @click="deleteContent(row)" :disabled="row.status === 'published'">删除</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="账号绑定" name="accounts">
        <div class="toolbar">
          <el-select v-model="accountForm.platform" style="width:150px">
            <el-option
              v-for="workflow in activeWorkflows"
              :key="workflow.platform"
              :label="`${workflow.platform_name} · ${workflowStatusText(workflow.status)}`"
              :value="workflow.platform"
            />
          </el-select>
          <el-input v-model="accountForm.account_name" placeholder="账号名称" style="width:220px" />
          <el-button type="primary" @click="createAccount" :loading="busy.createAccount">绑定账号</el-button>
        </div>
        <el-table :data="accounts" stripe>
          <el-table-column label="平台" width="120">
            <template #default="{ row }">{{ platformLabel(row.platform) }}</template>
          </el-table-column>
          <el-table-column prop="account_name" label="账号" width="180" />
          <el-table-column label="状态" width="170">
            <template #default="{ row }">
              <el-tag size="small" :type="publishStatusType(row.status)">{{ publishStatusText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="port" label="端口" width="80" />
          <el-table-column prop="profile_dir" label="浏览器登录态目录" min-width="260" show-overflow-tooltip />
          <el-table-column prop="publish_url" label="固定发布链接" min-width="220" show-overflow-tooltip />
          <el-table-column prop="notes" label="备注" min-width="220" />
          <el-table-column label="操作" width="240">
            <template #default="{ row }">
              <el-button size="small" @click="openAccount(row)">打开登录</el-button>
              <el-button size="small" type="primary" @click="checkAccount(row)">查状态</el-button>
              <el-button size="small" type="danger" @click="deleteAccount(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="发布任务" name="publish">
        <div class="filter-bar" style="margin:12px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <el-input v-model="publishFilter.keyword" placeholder="搜索内容标题/关键词" style="width:220px" clearable @clear="refreshPublishTasks" @keyup.enter="refreshPublishTasks" />
          <el-select v-model="publishFilter.status" placeholder="状态筛选" style="width:140px" clearable @change="refreshPublishTasks">
            <el-option label="待处理" value="pending" />
            <el-option label="素材就绪" value="assets_ready" />
            <el-option label="已准备" value="prepared" />
            <el-option label="运行中" value="running" />
            <el-option label="已发布" value="published" />
            <el-option label="失败" value="failed" />
          </el-select>
          <el-select v-model="publishFilter.platform" placeholder="平台筛选" style="width:150px" clearable @change="refreshPublishTasks">
            <el-option label="官网/落地页" value="website" />
            <el-option label="携程内容中心/旅拍" value="ctrip_travel" />
            <el-option label="抖音创作者中心" value="douyin" />
            <el-option label="今日头条号" value="toutiao" />
            <el-option label="百家号" value="baijiahao" />
            <el-option label="搜狐号" value="sohu" />
            <el-option label="知乎" value="zhihu" />
            <el-option label="小红书" value="xiaohongshu" />
          </el-select>
          <el-button type="primary" size="small" @click="refreshPublishTasks">搜索</el-button>
          <el-button size="small" @click="resetPublishFilter">重置</el-button>
          <span class="muted" style="font-size:12px">共 {{ publishTasks.length }} 条</span>
        </div>
        <el-table :data="publishTasks" stripe>
          <el-table-column prop="id" label="编号" width="70" />
          <el-table-column prop="title" label="内容标题" min-width="260" show-overflow-tooltip />
          <el-table-column label="平台" width="120">
            <template #default="{ row }">{{ platformLabel(row.platform) }}</template>
          </el-table-column>
          <el-table-column prop="account_name" label="账号" width="150" />
          <el-table-column label="状态" width="150">
            <template #default="{ row }">
              <el-tag size="small" :type="publishStatusType(row.status)">{{ publishStatusText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="workflow_status" label="工作流" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="workflowStatusType(row.workflow_status)">{{ workflowStatusText(row.workflow_status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="publish_gate_reason" label="发布门禁" min-width="160" show-overflow-tooltip />
          <el-table-column label="闭环" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="closureStatusType(row.closure_status)">{{ closureStatusText(row.closure_status) }}</el-tag>
              <el-tag v-if="row.workflow_needs_retest" size="small" type="warning">工作流待复测</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="上线链接" width="110">
            <template #default="{ row }">
              <a v-if="row.published_url_verified && row.published_url" :href="row.published_url" target="_blank">已验真</a>
              <el-tag v-else-if="row.published_url" size="small" type="warning">链接待验真</el-tag>
              <el-tag v-else size="small" type="info">未上线</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="素材证据" width="250">
            <template #default="{ row }">
              <a v-if="row.raw_artifact_verified && row.raw_artifact_path" :href="artifactUrl(row.raw_artifact_path)" target="_blank">网页原文</a>
              <span v-if="row.raw_artifact_verified && row.raw_artifact_path"> / </span>
              <a v-if="row.screenshot_verified && row.screenshot_path" :href="artifactUrl(row.screenshot_path)" target="_blank">截图</a>
              <span v-if="row.screenshot_verified && row.screenshot_path"> / </span>
              <a v-if="row.summary_artifact_verified && row.summary_artifact_path" :href="artifactUrl(row.summary_artifact_path)" target="_blank">摘要</a>
              <el-tag v-if="!hasMaterialEvidence(row)" size="small" type="danger">缺三件套</el-tag>
              <el-tag v-else-if="row.evidence_status !== 'verified'" size="small" type="warning">三件套已验真/未上线</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="failure_reason" label="失败原因" min-width="180" show-overflow-tooltip />
          <el-table-column prop="evidence_failure_reason" label="证据问题" min-width="180" show-overflow-tooltip />
          <el-table-column label="操作" width="430" fixed="right">
            <template #default="{ row }">
              <div class="table-actions">
                <el-button size="small" type="primary" @click="prepareTask(row)" :disabled="!canPrepareTask(row) || busy.prepareTask === row.id" :loading="busy.prepareTask === row.id">生成素材</el-button>
                <el-button size="small" type="success" @click="runPublishTask(row)" :disabled="!canRunTask(row) || busy.runPublishTask === row.id" :loading="busy.runPublishTask === row.id">运行自动填草稿</el-button>
                <el-button size="small" type="success" @click="autoVerifyWebsite(row)" :disabled="!canAutoVerifyWebsite(row)">自动验真</el-button>
                <el-button size="small" type="success" @click="recoverPublishUrl(row)" :disabled="!canRecoverPublishUrl(row)">回捞验真</el-button>
                <el-button size="small" type="warning" @click="confirmPublish(row)" :disabled="!canConfirmTask(row)">回填链接</el-button>
                <el-button size="small" @click="retestPublish(row)" :disabled="!canRetestTask(row)">复测</el-button>
                <el-button size="small" type="danger" @click="markPublishFail(row)" :disabled="row.status === 'published'">标失败</el-button>
                <el-button size="small" type="danger" @click="deletePublishTask(row)" :disabled="row.status === 'published' && row.published_url_verified">删除任务</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="成绩单" name="reports">
        <div class="toolbar">
          <el-button type="primary" @click="createReport" :disabled="!brandId">生成成绩单</el-button>
        </div>
        <el-table :data="reports" stripe>
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="created_at" label="生成时间" width="170" />
          <el-table-column label="已验证率" width="100">
            <template #default="{ row }">{{ row.summary?.verified_rate || 0 }}%</template>
          </el-table-column>
          <el-table-column label="证据率" width="100">
            <template #default="{ row }">{{ row.summary?.evidence_rate || 0 }}%</template>
          </el-table-column>
          <el-table-column label="发布准备率" width="110">
            <template #default="{ row }">{{ row.summary?.publish_ready_rate || 0 }}%</template>
          </el-table-column>
          <el-table-column label="报告" width="90">
            <template #default="{ row }">
              <a v-if="row.artifact_path" :href="artifactUrl(row.artifact_path)" target="_blank">网页原文</a>
            </template>
          </el-table-column>
          <el-table-column label="诊断" min-width="360">
            <template #default="{ row }">{{ (row.summary?.diagnosis || []).join('；') }}</template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="previewVisible" title="内容预览" width="720px">
      <h3>{{ preview.title }}</h3>
      <pre class="preview-body">{{ preview.body }}</pre>
    </el-dialog>

    <el-dialog v-model="editVisible" title="编辑内容" width="820px">
      <el-form label-width="80px">
        <el-form-item label="标题">
          <el-input v-model="editForm.title" />
        </el-form-item>
        <el-form-item label="正文">
          <el-input v-model="editForm.body" type="textarea" :rows="16" />
        </el-form-item>
        <el-form-item label="发布渠道">
          <el-select v-model="editForm.target_platform" style="width:260px" :disabled="!canEditPlatform">
            <el-option
              v-for="workflow in activeWorkflows"
              :key="workflow.platform"
              :label="`${workflow.platform_name} · ${workflowStatusText(workflow.status)}`"
              :value="workflow.platform"
            />
          </el-select>
          <span class="form-hint" style="margin-left:8px">
            {{ canEditPlatform ? '发布前可切换渠道（如改到已登录的知乎）' : '发布中/已发布的内容渠道不可修改' }}
          </span>
        </el-form-item>
        <el-form-item label="AI方向">
          <el-input
            v-model="editForm.direction"
            type="textarea"
            :rows="3"
            placeholder="例如：正文扩写到800字、加入本地旅游攻略语气、小红书风格、不要URL、不要竞品名、加强酒店位置优势"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="warning" plain @click="rewriteEditedContent" :disabled="!editForm.id || busy.rewriteContent === editForm.id" :loading="busy.rewriteContent === editForm.id">按方向AI重写</el-button>
        <el-button type="primary" @click="saveContent">保存</el-button>
      </template>
    </el-dialog>

    <input
      ref="mediaInput"
      type="file"
      multiple
      accept="image/jpeg,image/png,image/webp,video/mp4,video/quicktime"
      style="display:none"
      @change="uploadSelectedMedia"
    />

    <el-dialog v-model="workflowVisible" title="编辑发布平台规划" width="720px">
      <el-form label-width="120px">
        <el-form-item label="平台名称">
          <el-input v-model="workflowForm.platform_name" />
        </el-form-item>
        <el-form-item label="登录链接">
          <el-input v-model="workflowForm.login_url" placeholder="用户点击打开登录使用" />
        </el-form-item>
        <el-form-item label="固定发布链接">
          <el-input v-model="workflowForm.publish_url" placeholder="自动填草稿每次打开的发布入口" />
        </el-form-item>
        <el-form-item label="自动发布工作流（JSON）">
          <el-input
            v-model="workflowForm.workflow_json_text"
            type="textarea"
            :rows="9"
            placeholder="步骤示例：打开发布页、填标题、填正文、人工发布、验真链接（JSON 字段名保持英文）"
          />
        </el-form-item>
        <el-form-item label="当前状态">
          <el-tag :type="workflowStatusType(workflowForm.current_status)">{{ workflowStatusText(workflowForm.current_status) }}</el-tag>
          <span class="form-hint">已测试/启用只能由真实工作流测试产生，保存规划不会伪造状态。</span>
        </el-form-item>
        <el-form-item label="人工状态">
          <el-select v-model="workflowForm.manual_status" style="width:180px">
            <el-option label="不修改" value="" />
            <el-option label="草稿" value="draft" />
            <el-option label="模板待测试" value="template" />
            <el-option label="禁用" value="disabled" />
          </el-select>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="workflowForm.notes" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="workflowVisible = false">取消</el-button>
        <el-button type="primary" @click="saveWorkflow">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import authFetch from '../utils/authFetch'
import { evidenceUrl } from '../utils/evidenceUrl'
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getKeywords } from '../api/keywords'
import {
  platformLabel,
  publishStatusLabel,
  strategyStatusLabel,
  actionStatusLabel,
  riskLabel,
  executionLevelLabel,
  contentTypeLabel,
  ideaStatusLabel,
  knowledgeStatusLabel,
  categoryLabel,
} from '../utils/labels'

const tab = ref('strategy')
const brands = ref([])
const brandId = ref(null)
const seed = ref('')
const ideaCount = ref(30)
const strategyObjective = ref('')
const strategies = ref([])
const selectedStrategyActions = ref([])
const executingStrategy = ref(false)
const strategyExecutionSummary = ref(null)
const contentKeyword = ref('')
const ideas = ref([])
const ideaBatches = ref([])
const ideaBatchFilter = ref('')
const ideaValueFilter = ref('')
const ideaBrandFilter = ref('')
const latestGeneratedBatch = ref('')
const latestGeneratedBatchLabel = ref('')
const selectedIdeas = ref([])
const keywords = ref([])
const contents = ref([])
const contentFilter = reactive({ keyword: '', status: '', content_type: '', target_platform: '', source: '', brand_id: '' })
const accounts = ref([])
const workflows = ref([])
const ecosystems = ref([])
const knowledgeSummary = ref({})
const knowledgeRecords = ref([])
const knowledgeResource = ref('items')
const knowledgeResourceOptions = [
  { value: 'items', label: '品牌事实', file: 'brand_facts.json', hint: '品牌、产品、场景、地域、差异化卖点等可审计事实。' },
  { value: 'evidence', label: '证据源', file: 'evidence_sources.json', hint: '官网、媒体、行业平台、公开页面等可追溯来源。' },
  { value: 'questions', label: '问题矩阵', file: 'question_matrix.json', hint: '用户会问什么、意图是什么、应绑定哪些事实和信源。' },
  { value: 'diagnostics', label: '引用诊断', file: 'citation_diagnostics.json', hint: '记录 not_cited、unverified_citation、来源缺失等失败归因。' },
]
const workflowTests = ref([])
const testingWorkflow = ref('')
const discoveringWorkflow = ref('')
const selectedWorkflowPlatform = ref('')
const publishTasks = ref([])
const publishFilter = reactive({ keyword: '', status: '', platform: '' })
const reports = ref([])
const workbench = ref({})
const contentQuality = ref(null)
const mediaInput = ref(null)
const mediaTargetContent = ref(null)
const previewVisible = ref(false)
const preview = ref({})
const editVisible = ref(false)
const editForm = reactive({ id: null, title: '', body: '', direction: '', target_platform: '', status: '' })
const canEditPlatform = computed(() => !['publishing', 'published'].includes(editForm.status))
const workflowVisible = ref(false)
const workflowForm = reactive({ platform: '', platform_name: '', login_url: '', publish_url: '', workflow_json_text: '', current_status: 'template', manual_status: '', notes: '' })
const knowledgeForm = reactive({
  title: '',
  category: '',
  content: '',
  source_url: '',
  question: '',
  platform: '',
  status: 'active',
})
const busy = reactive({
  refreshAll: false,
  refreshWorkflows: false,
  refreshEcosystems: false,
  refreshKnowledge: false,
  saveKnowledge: false,
  generateStrategy: false,
  confirmStrategy: false,
  generateIdeas: false,
  adoptIdeas: false,
  generateContent: false,
  rewriteContent: null,
  approveContent: null,
  checkContentQuality: null,
  createAccount: false,
  createPublishTask: null,
  prepareTask: null,
  runPublishTask: null,
})

const contentForm = reactive({ keyword_id: null, content_type: 'guide', target_platform: 'website' })
const accountForm = reactive({ platform: 'website', account_name: '' })
const canGenerateContent = computed(() => !!brandId.value && (!!contentForm.keyword_id || !!contentKeyword.value.trim()))
const activeWorkflows = computed(() => workflows.value.filter(item => item.status !== 'disabled'))
const activeStrategy = computed(() => strategies.value[0] || null)
const activeKnowledgeResource = computed(() => knowledgeResourceOptions.find(item => item.value === knowledgeResource.value) || knowledgeResourceOptions[0])
const canSaveKnowledge = computed(() => !!brandId.value && !!knowledgeForm.title.trim())
const strategyActionStats = computed(() => {
  const stats = { confirmed: 0, executed: 0, blocked: 0, failed: 0, pending: 0 }
  for (const item of activeStrategy.value?.actions || []) {
    if (item.status === 'confirmed') stats.confirmed += 1
    else if (item.status === 'executed') stats.executed += 1
    else if (item.status === 'blocked') stats.blocked += 1
    else if (item.status === 'failed') stats.failed += 1
    else stats.pending += 1
  }
  return stats
})
const strategyFailedActions = computed(() => (activeStrategy.value?.actions || []).filter(item => item.status === 'failed'))
const strategyBlockedActions = computed(() => (activeStrategy.value?.actions || []).filter(item => item.status === 'blocked'))
const canExecuteStrategyActions = computed(() => {
  const actions = activeStrategy.value?.actions || []
  return actions.some(item => item.status === 'confirmed')
})

const apiJson = async (url, options = {}) => {
  try {
    const res = await authFetch(url, { headers: { 'Content-Type': 'application/json' }, ...options })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || data?.message || '请求失败')
    return data
  } catch (error) {
    ElMessage.error(error.message || '请求失败')
    throw error
  }
}

const apiUpload = async (url, formData) => {
  try {
    const res = await authFetch(url, { method: 'POST', body: formData })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || data?.message || '上传失败')
    return data
  } catch (error) {
    ElMessage.error(error.message || '上传失败')
    throw error
  }
}

const refreshContentsData = async () => {
  const params = new URLSearchParams({ limit: '100' })
  if (contentFilter.brand_id) params.set('brand_id', contentFilter.brand_id)
  if (contentFilter.keyword) params.set('keyword', contentFilter.keyword)
  if (contentFilter.status) params.set('status', contentFilter.status)
  if (contentFilter.content_type) params.set('content_type', contentFilter.content_type)
  if (contentFilter.target_platform) params.set('target_platform', contentFilter.target_platform)
  if (contentFilter.source) params.set('source', contentFilter.source)
  return apiJson(`/api/operations/content?${params.toString()}`)
}

const refreshContents = async () => {
  try {
    contents.value = await refreshContentsData()
  } catch (error) {
    ElMessage.error(`刷新内容列表失败: ${error.message}`)
  }
}

const resetContentFilter = async () => {
  Object.assign(contentFilter, { keyword: '', status: '', content_type: '', target_platform: '', source: '', brand_id: '' })
  await refreshContents()
}

const refreshPublishTasksData = async () => {
  const params = new URLSearchParams({ brand_id: String(brandId.value), limit: '100' })
  if (publishFilter.keyword) params.set('keyword', publishFilter.keyword)
  if (publishFilter.status) params.set('status', publishFilter.status)
  if (publishFilter.platform) params.set('platform', publishFilter.platform)
  return apiJson(`/api/operations/publish/tasks?${params.toString()}`)
}

const refreshPublishTasks = async () => {
  if (!brandId.value) return
  try {
    publishTasks.value = await refreshPublishTasksData()
  } catch (error) {
    ElMessage.error(`刷新发布任务失败: ${error.message}`)
  }
}

const resetPublishFilter = async () => {
  Object.assign(publishFilter, { keyword: '', status: '', platform: '' })
  await refreshPublishTasks()
}

const refreshAll = async () => {
  if (!brandId.value) return
  busy.refreshAll = true
  try {
    await refreshWorkflows()
    await refreshEcosystems()
    await refreshIdeaBatches()
    await refreshIdeas()
    const kwRes = await getKeywords({ brand_id: brandId.value, page_size: 200 })
    keywords.value = kwRes.items || kwRes.list || kwRes || []
    contents.value = await refreshContentsData()
    accounts.value = await apiJson('/api/operations/accounts')
    publishTasks.value = await refreshPublishTasksData()
    reports.value = await apiJson(`/api/operations/reports?brand_id=${brandId.value}&limit=20`)
    workbench.value = await apiJson(`/api/operations/workbench?brand_id=${brandId.value}`)
    await refreshStrategies()
    await refreshKnowledge()
    contentQuality.value = null
  } finally {
    busy.refreshAll = false
  }
}

const refreshKnowledge = async () => {
  busy.refreshKnowledge = true
  try {
    knowledgeSummary.value = await apiJson('/api/operations/knowledge')
    await refreshKnowledgeRecords()
  } finally {
    busy.refreshKnowledge = false
  }
}

const refreshKnowledgeRecords = async () => {
  if (!brandId.value) return
  const params = new URLSearchParams({ brand_id: String(brandId.value) })
  knowledgeRecords.value = await apiJson(`/api/operations/knowledge/${knowledgeResource.value}?${params.toString()}`)
}

const resetKnowledgeForm = () => {
  Object.assign(knowledgeForm, {
    title: '',
    category: '',
    content: '',
    source_url: '',
    question: '',
    platform: '',
    status: 'active',
  })
}

const saveKnowledgeRecord = async () => {
  if (!canSaveKnowledge.value) return ElMessage.warning('先选择品牌并填写标题')
  busy.saveKnowledge = true
  try {
    const payload = {
      brand_id: brandId.value,
      title: knowledgeForm.title.trim(),
      category: knowledgeForm.category.trim() || undefined,
      content: knowledgeForm.content.trim() || undefined,
      source_url: knowledgeForm.source_url.trim() || undefined,
      question: knowledgeForm.question.trim() || undefined,
      platform: knowledgeForm.platform.trim() || undefined,
      status: knowledgeForm.status || 'active',
      metadata: {
        resource_file: activeKnowledgeResource.value.file,
        source: 'operations_ui',
      },
    }
    await apiJson(`/api/operations/knowledge/${knowledgeResource.value}`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
    ElMessage.success('知识记录已写入项目内 geo/knowledge_base')
    resetKnowledgeForm()
    await refreshKnowledge()
  } finally {
    busy.saveKnowledge = false
  }
}

const refreshIdeaBatches = async () => {
  if (!brandId.value) return
  ideaBatches.value = await apiJson(`/api/operations/keywords/idea-batches?brand_id=${brandId.value}&limit=50`)
  if (!latestGeneratedBatch.value && ideaBatches.value.length) {
    latestGeneratedBatch.value = ideaBatches.value[0].generation_batch
    latestGeneratedBatchLabel.value = ideaBatches.value[0].batch_label
  }
}

const refreshIdeas = async () => {
  if (!brandId.value) return
  const params = new URLSearchParams({ brand_id: String(brandId.value), limit: '100' })
  if (ideaBatchFilter.value === '__latest__') params.set('latest_batch', 'true')
  else if (ideaBatchFilter.value) params.set('batch', ideaBatchFilter.value)
  if (ideaValueFilter.value) params.set('geo_value_type', ideaValueFilter.value)
  if (ideaBrandFilter.value) params.set('brand_mention_type', ideaBrandFilter.value)
  ideas.value = await apiJson(`/api/operations/keywords/ideas?${params.toString()}`)
}

const refreshStrategies = async () => {
  if (!brandId.value) return
  strategies.value = await apiJson(`/api/operations/strategy?brand_id=${brandId.value}&limit=5`)
  selectedStrategyActions.value = []
}

const refreshWorkflows = async () => {
  busy.refreshWorkflows = true
  try {
    workflows.value = await apiJson('/api/operations/publish/workflows')
    if (!activeWorkflows.value.some(item => item.platform === contentForm.target_platform)) {
      contentForm.target_platform = activeWorkflows.value[0]?.platform || 'website'
    }
    if (!activeWorkflows.value.some(item => item.platform === accountForm.platform)) {
      accountForm.platform = activeWorkflows.value[0]?.platform || 'website'
    }
    if (!workflows.value.some(item => item.platform === selectedWorkflowPlatform.value)) {
      selectedWorkflowPlatform.value = workflows.value.find(item => item.platform !== 'website')?.platform || workflows.value[0]?.platform || ''
    }
    if (selectedWorkflowPlatform.value) await loadWorkflowTestsByPlatform(selectedWorkflowPlatform.value)
  } finally {
    busy.refreshWorkflows = false
  }
}

const loadWorkflowTestsByPlatform = async (platform) => {
  if (!platform) return
  selectedWorkflowPlatform.value = platform
  workflowTests.value = await apiJson(`/api/operations/publish/workflows/${platform}/tests`)
}

const loadWorkflowTests = async (row) => {
  await loadWorkflowTestsByPlatform(row.platform)
}

const refreshEcosystems = async () => {
  busy.refreshEcosystems = true
  try {
    ecosystems.value = await apiJson(`/api/operations/publish/ecosystems?brand_id=${brandId.value}`)
  } finally {
    busy.refreshEcosystems = false
  }
}

const changeBrand = async () => {
  localStorage.setItem('geo_ops_brand_id', String(brandId.value))
  strategyExecutionSummary.value = null
  ideaBatchFilter.value = ''
  ideaValueFilter.value = ''
  ideaBrandFilter.value = ''
  latestGeneratedBatch.value = ''
  latestGeneratedBatchLabel.value = ''
  await refreshAll()
}

const generateStrategy = async () => {
  localStorage.setItem('geo_ops_brand_id', String(brandId.value))
  busy.generateStrategy = true
  const tip = ElMessage.info({ message: 'AI策略生成中，预计 10-30 秒；完成后会自动刷新策略和执行项', duration: 0 })
  try {
    const data = await apiJson('/api/operations/strategy/generate', {
      method: 'POST',
      body: JSON.stringify({ brand_id: brandId.value, objective: strategyObjective.value.trim() }),
    })
    tip.close()
    ElMessage.success(`AI策略已生成：${data.actions?.length || 0} 个执行项，模型 ${data.llm_model || '未知'}，Token ${data.total_tokens || 0}`)
    await refreshStrategies()
  } finally {
    tip.close()
    busy.generateStrategy = false
  }
}

const confirmStrategyActions = async () => {
  if (!activeStrategy.value) return
  const ids = selectedStrategyActions.value.map(item => item.id)
  busy.confirmStrategy = true
  try {
    const data = await apiJson(`/api/operations/strategy/${activeStrategy.value.id}/actions/confirm`, {
      method: 'POST',
      body: JSON.stringify({ action_ids: ids }),
    })
    ElMessage.success(`已确认 ${data.confirmed_count} 个执行项；P3 未自动执行副作用`)
    await refreshStrategies()
  } finally {
    busy.confirmStrategy = false
  }
}

const executeStrategyActions = async () => {
  if (!activeStrategy.value) return
  if (!canExecuteStrategyActions.value) return ElMessage.warning('先勾选执行项并点击确认选中执行项')
  executingStrategy.value = true
  try {
    await ElMessageBox.confirm('只执行已确认的白名单动作。外部平台最终发布仍需人工确认。', '执行AI策略', {
      confirmButtonText: '执行',
      cancelButtonText: '取消',
      type: 'warning',
    })
    const data = await apiJson(`/api/operations/strategy/${activeStrategy.value.id}/actions/execute`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    strategyExecutionSummary.value = data
    await refreshAll()
    const blockedLines = strategyBlockedActions.value
      .map(item => `${item.title || actionTypeText(item.action_type)}：${item.failure_reason || '缺少前置条件，等待人工处理'}`)
      .slice(0, 8)
      .join('\n')
    const failedLines = strategyFailedActions.value
      .map(item => `${item.title || actionTypeText(item.action_type)}：${item.failure_reason || '执行失败'}`)
      .slice(0, 8)
      .join('\n')
    const message = [
      `成功：${data.executed_count}`,
      `待处理：${data.blocked_count || 0}`,
      `失败：${data.failed_count}`,
      blockedLines ? `\n待处理原因：\n${blockedLines}` : '',
      failedLines ? `\n失败原因：\n${failedLines}` : '',
      (data.failed_count || data.blocked_count) ? '\n下一步：先审核通过内容，再创建发布任务；发布后回填真实链接，再做自动填草稿和复测。' : '',
    ].filter(Boolean).join('\n')
    await ElMessageBox.alert(message, '执行结果摘要', {
      confirmButtonText: '知道了',
      type: data.failed_count ? 'error' : ((data.blocked_count || 0) ? 'warning' : 'success'),
    })
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e?.message || '策略执行失败')
  } finally {
    executingStrategy.value = false
  }
}

const generateIdeas = async () => {
  busy.generateIdeas = true
  const tip = ElMessage.info({ message: `关键词生成中，预计 5-20 秒；将基于种子词扩展 GEO 问法`, duration: 0 })
  try {
    const data = await apiJson('/api/operations/keywords/generate', {
      method: 'POST',
      body: JSON.stringify({ brand_id: brandId.value, seed: seed.value, count: ideaCount.value }),
    })
    tip.close()
    latestGeneratedBatch.value = data.generation_batch || ''
    latestGeneratedBatchLabel.value = data.batch_label || data.generation_batch || ''
    ideaBatchFilter.value = data.generation_batch || '__latest__'
    ElMessage.success(`AI生成 ${data.count} 个关键词建议；已切换到最新批次：${latestGeneratedBatchLabel.value || '最新批次'}`)
    await refreshIdeaBatches()
    await refreshIdeas()
  } finally {
    tip.close()
    busy.generateIdeas = false
  }
}

const adoptSelected = async () => {
  const ids = selectedIdeas.value.map(x => x.id)
  busy.adoptIdeas = true
  try {
    const data = await apiJson('/api/operations/keywords/adopt', {
      method: 'POST',
      body: JSON.stringify({ idea_ids: ids }),
    })
    ElMessage.success(`采纳 ${data.count} 个关键词`)
    await refreshAll()
  } finally {
    busy.adoptIdeas = false
  }
}

const generateContent = async () => {
  if (!canGenerateContent.value) return ElMessage.warning('先选择关键词或输入真实用户问题')
  busy.generateContent = true
  const tip = ElMessage.info({ message: 'AI内容生成中，预计 10-40 秒；系统会检查平台规则和正文 URL 风险', duration: 0 })
  try {
    const data = await apiJson('/api/operations/content/generate', {
      method: 'POST',
      body: JSON.stringify({ brand_id: brandId.value, ...contentForm, keyword: contentKeyword.value.trim() }),
    })
    tip.close()
    ElMessage.success(`AI草稿已生成，质量分 ${data.quality_score}，模型 ${data.llm_model || '未知'}，Token ${data.total_tokens || 0}`)
    await refreshAll()
  } finally {
    tip.close()
    busy.generateContent = false
  }
}

const previewContent = (row) => {
  preview.value = row
  previewVisible.value = true
}

const editContent = (row) => {
  Object.assign(editForm, {
    id: row.id,
    title: row.title,
    body: row.body,
    direction: '',
    target_platform: row.target_platform || 'website',
    status: row.status || '',
  })
  editVisible.value = true
}

const saveContent = async () => {
  const payload = { title: editForm.title, body: editForm.body }
  if (canEditPlatform.value && editForm.target_platform) {
    payload.target_platform = editForm.target_platform
  }
  await apiJson(`/api/operations/content/${editForm.id}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
  ElMessage.success('内容已保存')
  editVisible.value = false
  await refreshAll()
  await checkContentQuality({ id: editForm.id })
}

const rewriteEditedContent = async () => {
  if (!editForm.id) return
  const direction = editForm.direction.trim()
  if (!direction) return ElMessage.warning('先填写 AI 修改方向')
  busy.rewriteContent = editForm.id
  const tip = ElMessage.info({ message: 'AI按方向重写中，预计 10-60 秒；完成后会回填编辑框并复查门禁', duration: 0 })
  try {
    const data = await apiJson(`/api/operations/content/${editForm.id}/rewrite`, {
      method: 'POST',
      body: JSON.stringify({ mode: 'rewrite', direction }),
    })
    Object.assign(editForm, { title: data.title, body: data.body })
    tip.close()
    ElMessage.success(`AI重写完成，质量分 ${data.quality_score || '-'}，模型 ${data.llm_model || '未知'}`)
    await refreshAll()
    await checkContentQuality({ id: editForm.id })
  } finally {
    tip.close()
    busy.rewriteContent = null
  }
}

const rewriteContent = async (row, mode) => {
  const labels = {
    rewrite: 'AI重写',
    fix_gate: '按门禁修复',
    regenerate: '重新生成',
  }
  try {
    let direction = ''
    if (mode !== 'fix_gate') {
      const prompt = await ElMessageBox.prompt(
        '输入生成方向，例如：更像小红书、正文800字以上、加强本地旅游攻略、不要竞品名、不要URL。',
        labels[mode] || 'AI重写',
        {
          confirmButtonText: '开始',
          cancelButtonText: '取消',
          inputPlaceholder: '可留空，系统按当前品牌/关键词/平台生成',
        },
      )
      direction = (prompt.value || '').trim()
    } else {
      const quality = await apiJson(`/api/operations/content/${row.id}/quality-check?ai=false`)
      contentQuality.value = quality
      direction = `只修复这些门禁问题：${(quality.blocking_issues || quality.issues || []).join('；')}`
    }
    busy.rewriteContent = row.id
    const tip = ElMessage.info({ message: `${labels[mode]}中，预计 10-60 秒；完成后会重新检查发布门禁`, duration: 0 })
    try {
      const data = await apiJson(`/api/operations/content/${row.id}/rewrite`, {
        method: 'POST',
        body: JSON.stringify({ mode, direction }),
      })
      tip.close()
      ElMessage.success(`${labels[mode]}完成，质量分 ${data.quality_score || '-'}，模型 ${data.llm_model || '未知'}`)
      await refreshAll()
      await checkContentQuality({ id: row.id })
    } finally {
      tip.close()
      busy.rewriteContent = null
    }
  } catch (e) {
    busy.rewriteContent = null
    if (e !== 'cancel') ElMessage.error(e?.message || `${labels[mode]}失败`)
  }
}

const approveContent = async (row) => {
  if (busy.approveContent) return
  busy.approveContent = row.id
  try {
    // ai=false 只跑本地硬门禁（秒回）：AI复审不阻断审核，没必要让用户等几十秒
    const quality = await apiJson(`/api/operations/content/${row.id}/quality-check?ai=false`)
    contentQuality.value = quality
    if (!quality.hard_passed) {
      const issues = quality.blocking_issues?.length ? quality.blocking_issues : quality.issues
      ElMessage.warning(`内容未通过质量检查：${(issues || []).join('；')}`)
      return
    }
    await apiJson(`/api/operations/content/${row.id}/approve`, { method: 'POST' })
    ElMessage.success('内容已审核通过')
    await refreshAll()
  } finally {
    busy.approveContent = null
  }
}

const checkContentQuality = async (row) => {
  if (busy.checkContentQuality) return
  busy.checkContentQuality = row.id
  try {
    const data = await apiJson(`/api/operations/content/${row.id}/quality-check`)
    contentQuality.value = data
    const prefix = data.mechanism ? `${data.mechanism}；` : ''
    if (data.passed) ElMessage.success(`${prefix}质量检查通过`)
    else ElMessage.warning(`质量检查未通过：${(data.issues || []).slice(0, 4).join('；')}`)
  } finally {
    busy.checkContentQuality = null
  }
}

const parseMedia = (row) => {
  try {
    const items = JSON.parse(row.media_assets_json || '[]')
    return Array.isArray(items) ? items : []
  } catch {
    return []
  }
}

// 与后端 operations.COVER_REQUIRED_PLATFORMS 保持一致：发布时强制要封面图的平台
const COVER_REQUIRED_PLATFORMS = ['zhihu', 'baijiahao']
const coverImageCount = (row) => parseMedia(row).filter(item => item.kind === 'image').length


const mediaCount = (row) => parseMedia(row).length

const selectMedia = (row, folderMode = false) => {
  mediaTargetContent.value = row
  if (mediaInput.value) {
    mediaInput.value.value = ''
    if (folderMode) {
      mediaInput.value.setAttribute('webkitdirectory', '')
      mediaInput.value.setAttribute('directory', '')
    } else {
      mediaInput.value.removeAttribute('webkitdirectory')
      mediaInput.value.removeAttribute('directory')
    }
    mediaInput.value.click()
  }
}

const uploadSelectedMedia = async (event) => {
  const row = mediaTargetContent.value
  const files = Array.from(event.target.files || [])
  if (!row || !files.length) return
  const allowed = ['jpg', 'jpeg', 'png', 'webp', 'mp4', 'mov', 'm4v']
  const imageExts = ['jpg', 'jpeg', 'png', 'webp']
  const videoExts = ['mp4', 'mov', 'm4v']
  const existing = parseMedia(row)
  let imageCount = existing.filter(item => item.kind === 'image').length
  let videoCount = existing.filter(item => item.kind === 'video').length
  const validFiles = files.filter(file => allowed.includes((file.name.split('.').pop() || '').toLowerCase()))
  const invalidFiles = files.filter(file => !allowed.includes((file.name.split('.').pop() || '').toLowerCase()))
  if (invalidFiles.length) ElMessage.warning(`已跳过不支持格式：${invalidFiles.map(file => file.name).slice(0, 5).join('、')}`)
  if (!validFiles.length) {
    event.target.value = ''
    return ElMessage.warning('没有可上传的图片/视频文件')
  }
  const acceptedFiles = []
  const rejectedReasons = []
  for (const file of validFiles) {
    const ext = (file.name.split('.').pop() || '').toLowerCase()
    if (imageExts.includes(ext)) {
      if (imageCount >= 9) {
        rejectedReasons.push('图片最多9张')
        continue
      }
      if (file.size > 10 * 1024 * 1024) {
        rejectedReasons.push(`${file.name} 超过10MB`)
        continue
      }
      imageCount += 1
    } else if (videoExts.includes(ext)) {
      if (videoCount >= 1) {
        rejectedReasons.push('视频最多1个')
        continue
      }
      if (file.size > 200 * 1024 * 1024) {
        rejectedReasons.push(`${file.name} 超过200MB`)
        continue
      }
      videoCount += 1
    }
    acceptedFiles.push(file)
  }
  if (rejectedReasons.length) ElMessage.warning([...new Set(rejectedReasons)].slice(0, 4).join('；'))
  if (!acceptedFiles.length) {
    event.target.value = ''
    return ElMessage.warning('素材不符合平台上传限制')
  }
  const form = new FormData()
  acceptedFiles.forEach(file => form.append('files', file))
  try {
    const data = await apiUpload(`/api/operations/content/${row.id}/media`, form)
    const mediaInfo = (data.saved || []).map(item => item.kind === 'image' ? `${item.filename}(${item.width}x${item.height})` : `${item.filename}(${Math.round(item.size / 1024 / 1024)}MB)`).join('、')
    ElMessage.success(`已保存素材 ${data.saved?.length || 0} 个；${mediaInfo || '图片最多9张，视频最多1个'}`)
    await refreshAll()
  } finally {
    event.target.value = ''
  }
}

const clearMedia = async (row) => {
  await apiJson(`/api/operations/content/${row.id}/media`, { method: 'DELETE' })
  ElMessage.success('素材已清空')
  await refreshAll()
}

const deleteContent = async (row) => {
  try {
    await ElMessageBox.confirm(
      `删除内容「${row.title}」？未成功发布的关联任务会一起删除，已验真发布成功的内容禁止删除。`,
      '删除内容',
      {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
    const data = await apiJson(`/api/operations/content/${row.id}`, { method: 'DELETE' })
    ElMessage.success(`内容已删除；关联任务 ${data.deleted_publish_tasks || 0} 条，素材 ${data.removed_media || 0} 个`)
    await refreshAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e?.message || '删除内容失败')
  }
}

const createAccount = async () => {
  if (!accountForm.account_name) return ElMessage.warning('请输入账号名称')
  busy.createAccount = true
  try {
    await apiJson('/api/operations/accounts', { method: 'POST', body: JSON.stringify(accountForm) })
    ElMessage.success('账号已绑定；下一步请打开登录浏览器确认账号状态')
    await refreshAll()
  } finally {
    busy.createAccount = false
  }
}

const editWorkflow = (row) => {
  Object.assign(workflowForm, {
    platform: row.platform,
    platform_name: row.platform_name,
    login_url: row.login_url || '',
    publish_url: row.publish_url || '',
    workflow_json_text: JSON.stringify(row.workflow || { steps: ['open_publish_url', 'fill_title', 'fill_body', 'manual_publish', 'verify_url'], selectors: {}, requires_manual_publish: true }, null, 2),
    current_status: row.status || 'template',
    manual_status: '',
    notes: row.notes || '',
  })
  workflowVisible.value = true
}

const saveWorkflow = async () => {
  let workflowJson
  try {
    workflowJson = JSON.parse(workflowForm.workflow_json_text || '{}')
  } catch {
    ElMessage.error('自动发布工作流 JSON 格式错误')
    return
  }
  const steps = Array.isArray(workflowJson.steps) ? workflowJson.steps : []
  const isWebsite = workflowForm.platform === 'website'
  const hasRequiredSteps = isWebsite
    ? steps.includes('generate_html') && steps.includes('manual_deploy')
    : steps.includes('open_publish_url') && steps.includes('manual_publish')
  if (!hasRequiredSteps || workflowJson.requires_manual_publish !== true) {
    ElMessage.error(isWebsite
      ? '官网工作流必须包含 generate_html、manual_deploy，且 requires_manual_publish 必须为 true'
      : '外部平台工作流必须包含 open_publish_url、manual_publish，且 requires_manual_publish 必须为 true')
    return
  }
  const payload = {
    platform_name: workflowForm.platform_name,
    login_url: workflowForm.login_url,
    publish_url: workflowForm.publish_url,
    workflow_json: workflowJson,
    notes: workflowForm.notes,
  }
  if (workflowForm.manual_status) payload.status = workflowForm.manual_status
  await apiJson(`/api/operations/publish/workflows/${workflowForm.platform}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
  ElMessage.success('发布平台规划已保存')
  workflowVisible.value = false
  await refreshAll()
}

const testWorkflow = async (row) => {
  if (!canTestWorkflow(row)) return ElMessage.warning(workflowTestGateReason(row))
  testingWorkflow.value = row.platform
  try {
    const data = await apiJson(`/api/operations/publish/workflows/${row.platform}/test`, { method: 'POST' })
    if (data.status === 'prepared') ElMessage.success('工作流测试通过，已保存证据')
    else if (data.status === 'login_required') ElMessage.warning('固定发布链接可打开，但需要登录或验证码')
    else if (data.status === 'manual_required') ElMessage.warning('已保存页面证据，但需要人工固化选择器')
    else ElMessage.error(data.failure_reason || '工作流测试失败')
    await loadWorkflowTests(row)
    await refreshWorkflows()
  } finally {
    testingWorkflow.value = ''
  }
}

const discoverWorkflowSelectors = async (row) => {
  if (row.platform === 'website') return ElMessage.warning('官网/落地页不需要外部发布页选择器')
  discoveringWorkflow.value = row.platform
  try {
    const data = await apiJson(`/api/operations/publish/workflows/${row.platform}/discover-selectors`, {
      method: 'POST',
      body: JSON.stringify({ save: true }),
    })
    if (data.status === 'ready' && data.saved) ElMessage.success('已识别并保存 title/body 选择器，请继续执行测试')
    else if (data.status === 'login_required') ElMessage.warning('发布页需要登录或验证码，已保存证据')
    else ElMessage.warning(data.failure_reason || '未识别到完整选择器，请人工补充')
    await refreshWorkflows()
  } finally {
    discoveringWorkflow.value = ''
  }
}

const openWorkflowLogin = async (row) => {
  await apiJson(`/api/operations/publish/workflows/${row.platform}/open-login`, { method: 'POST' })
  ElMessage.success('平台登录浏览器已打开')
}

const openWorkflowPublish = async (row) => {
  await apiJson(`/api/operations/publish/workflows/${row.platform}/open-publish`, { method: 'POST' })
  ElMessage.success('固定发布页已打开')
}

const openAccount = async (row) => {
  const data = await apiJson(`/api/operations/accounts/${row.id}/open-login`, { method: 'POST' })
  const name = platformLabel(row.platform)
  if (data.status === 'logged_in') ElMessage.success(`${name}浏览器已打开，已登录（检测到登录凭证）`)
  else if (data.login_state === 'logged_out') ElMessage.warning(`${name}浏览器已打开，当前未登录，请在浏览器中完成登录`)
  else ElMessage.success(`${name}发布浏览器已打开，请在浏览器窗口确认登录状态`)
  await refreshAll()
}

const checkAccount = async (row) => {
  const data = await apiJson(`/api/operations/accounts/${row.id}/status`)
  const name = platformLabel(row.platform)
  if (data.status === 'logged_in') {
    ElMessage.success(`${name}：已登录，登录态正常`)
  } else if (data.login_state === 'logged_out') {
    ElMessage.warning(`${name}：浏览器已开但未登录，请在打开的浏览器中完成登录后重新查状态`)
  } else if (data.status === 'browser_open') {
    ElMessage.warning(`${name}：浏览器已连接，登录态无法自动确认（平台暂不支持凭证检测），请在浏览器窗口中查看`)
  } else if (data.status === 'not_connected') {
    ElMessage.info(`${name}：发布浏览器未开启，点"打开登录"后再查状态`)
  } else {
    ElMessage.info(`${name}：${publishStatusLabel(data.status) || data.status}`)
  }
  await refreshAll()
}

const deleteAccount = async (row) => {
  try {
    await ElMessageBox.confirm(`删除发布账号：${row.account_name}`, '删除账号', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await apiJson(`/api/operations/accounts/${row.id}`, { method: 'DELETE' })
    ElMessage.success('账号已删除')
    await refreshAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e?.message || '删除账号失败')
  }
}

const createPublishTask = async (row) => {
  if (row.status !== 'approved') return ElMessage.warning('内容审核通过后才能创建发布任务')
  const workflow = getWorkflow(row.target_platform)
  if (!workflow) return ElMessage.warning('发布平台工作流不存在')
  if (row.target_platform !== 'website' && !['tested', 'active'].includes(workflow.status)) {
    return ElMessage.warning(`${platformLabel(row.target_platform)}工作流未测试通过；请到“发布平台规划”打开发布页/上传必填素材并完成测试`)
  }
  if (row.target_platform !== 'website' && !workflow.publish_url) {
    return ElMessage.warning('外部平台需先配置固定发布 URL')
  }
  const account = accounts.value.find(x => x.platform === row.target_platform)
  if (row.target_platform !== 'website' && !account) {
    return ElMessage.warning('外部平台必须先绑定账号并打开登录，再创建发布任务')
  }
  busy.createPublishTask = row.id
  try {
    const tip = row.target_platform === 'website'
      ? '发布任务创建中；官网会生成可验真的落地页素材'
      : `发布任务创建中；${platformLabel(row.target_platform)}仍需运行自动填草稿，最终发布需人工确认`
    ElMessage.info(tip)
    await apiJson('/api/operations/publish/tasks', {
      method: 'POST',
      body: JSON.stringify({ content_id: row.id, platform: row.target_platform, account_id: account?.id || null }),
    })
    ElMessage.success('发布任务已创建；请切到“发布任务”继续生成素材/运行自动填草稿')
    tab.value = 'publish'
    await refreshAll()
  } finally {
    busy.createPublishTask = null
  }
}

const confirmPublish = async (row) => {
  try {
    const { value } = await ElMessageBox.prompt('粘贴真实发布后的 URL', '回填发布链接', {
      confirmButtonText: '确认发布',
      cancelButtonText: '取消',
      inputPattern: /^https?:\/\/.+/i,
      inputErrorMessage: '必须是 http:// 或 https:// 开头',
    })
    const data = await apiJson(`/api/operations/publish/tasks/${row.id}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ published_url: value, trigger_monitor: true }),
    })
    ElMessage.success(data.monitor_task ? `已发布，并提交复测任务 ${data.monitor_task.task_id}` : '已发布')
    await refreshAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e?.message || '回填发布链接失败')
  }
}

const autoVerifyWebsite = async (row) => {
  try {
    const data = await apiJson(`/api/operations/publish/tasks/${row.id}/auto-verify`, {
      method: 'POST',
      body: JSON.stringify({ trigger_monitor: true }),
    })
    ElMessage.success(data.monitor_task ? `官网已自动验真，并提交复测任务 ${data.monitor_task.task_id}` : '官网已自动验真')
    await refreshAll()
  } catch (e) {
    ElMessage.error(e?.message || '官网自动验真失败')
  }
}

const recoverPublishUrl = async (row) => {
  try {
    const manualSupported = ['zhihu', 'baijiahao', 'ctrip_travel', 'toutiao'].includes(row.platform)
    let publishedUrl = ''
    if (manualSupported) {
      try {
        const prompt = await ElMessageBox.prompt(
          '可留空自动回捞；如果你已经打开真实作品页，也可以直接粘贴 URL 立即验真。',
          `${platformLabel(row.platform)}发布URL回捞验真`,
          {
            confirmButtonText: '开始验真',
            cancelButtonText: '取消',
            inputPlaceholder: '可选：粘贴真实发布后的URL',
          }
        )
        publishedUrl = (prompt.value || '').trim()
        if (publishedUrl && !/^https?:\/\//i.test(publishedUrl)) {
          return ElMessage.error('发布URL必须以 http:// 或 https:// 开头')
        }
      } catch (e) {
        if (e === 'cancel') return
        throw e
      }
    }
    const loading = ElMessage.info({ message: publishedUrl ? '正在验真你粘贴的发布URL...' : '正在自动回捞发布URL并验真...', duration: 0 })
    const data = await apiJson(`/api/operations/publish/tasks/${row.id}/recover-url`, {
      method: 'POST',
      body: JSON.stringify({ published_url: publishedUrl || null, trigger_monitor: true }),
    })
    loading.close()
    if (data.status === 'verified') {
      ElMessage.success(data.monitor_task ? `发布URL已回捞验真，并提交复测任务 ${data.monitor_task.task_id}` : '发布URL已回捞验真')
    } else {
      const count = data.recovery?.candidates?.length || data.candidates?.length || 0
      ElMessage.warning(`${data.failure_reason || '未回捞到可验真的发布URL'}${count ? `；候选 ${count} 条，请人工确认后粘贴URL重试` : ''}`)
    }
    await refreshAll()
  } catch (e) {
    ElMessage.error(e?.message || '回捞验真失败')
  }
}

const retestPublish = async (row) => {
  if (!canRetestTask(row)) return ElMessage.warning('先回填真实发布链接，再做发布后复测')
  const data = await apiJson(`/api/operations/publish/tasks/${row.id}/retest`, { method: 'POST' })
  ElMessage.success(`复测任务已提交：${data.task_id}`)
  await refreshAll()
}

const markPublishFail = async (row) => {
  try {
    const { value } = await ElMessageBox.prompt('填写失败原因', '标记发布失败', {
      confirmButtonText: '确认',
      cancelButtonText: '取消',
      inputPattern: /.+/,
      inputErrorMessage: '失败原因不能为空',
    })
    await apiJson(`/api/operations/publish/tasks/${row.id}/fail`, {
      method: 'POST',
      body: JSON.stringify({ failure_reason: value }),
    })
    ElMessage.warning('已标记失败')
    await refreshAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e?.message || '标记失败失败')
  }
}

const deletePublishTask = async (row) => {
  try {
    await ElMessageBox.confirm(
      `删除发布任务 #${row.id}？会释放对应内容，允许重新编辑和重新创建发布任务；已验真发布成功的任务不能删除。`,
      '删除发布任务',
      {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
    await apiJson(`/api/operations/publish/tasks/${row.id}`, { method: 'DELETE' })
    ElMessage.success('发布任务已删除，内容已释放为可重新发布')
    await refreshAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e?.message || '删除发布任务失败')
  }
}

const prepareTask = async (row) => {
  if (!canPrepareTask(row)) return ElMessage.warning('当前状态不能生成素材')
  busy.prepareTask = row.id
  const tip = ElMessage.info({ message: '正在生成本地 HTML/截图/摘要（不会打开平台浏览器）', duration: 0 })
  try {
    const data = await apiJson(`/api/operations/publish/tasks/${row.id}/prepare`, { method: 'POST' })
    tip.close()
    ElMessage.success(`本地素材已生成（未跑自动填草稿）：${data.raw_artifact_path}`)
    await refreshAll()
  } finally {
    tip.close()
    busy.prepareTask = null
  }
}

const runPublishTask = async (row) => {
  if (!canRunTask(row)) return ElMessage.warning('当前状态不能运行自动填草稿')
  busy.runPublishTask = row.id
  const tip = ElMessage.info({ message: `${platformLabel(row.platform)} 自动填草稿运行中；会打开固定发布页、填草稿并保存真实页面证据`, duration: 0 })
  try {
    const data = await apiJson(`/api/operations/publish/tasks/${row.id}/run`, { method: 'POST' })
    if (data.async_run || data.status === 'running') {
      ElMessage.success('自动填草稿已提交后台执行，正在轮询结果')
      const finalTask = await waitPublishTask(row.id)
      tip.close()
      if (finalTask.status === 'prepared') ElMessage.success('自动填草稿已完成并保存证据；请到平台页面人工确认最终发布')
      else if (finalTask.status === 'manual_required') ElMessage.warning(finalTask.failure_reason || '平台存在必填项，需人工处理；系统已保存证据')
      else if (finalTask.status === 'login_required') ElMessage.warning('账号需登录或验证，已保存证据')
      else ElMessage.error(finalTask.failure_reason || '自动填草稿失败')
      await refreshAll()
      return
    }
    tip.close()
    if (data.status === 'prepared') ElMessage.success('自动填草稿已完成并保存证据；请到平台页面人工确认最终发布')
    else if (data.status === 'manual_required') ElMessage.warning(data.failure_reason || '平台存在必填项，需人工处理；系统已保存证据')
    else if (data.status === 'login_required') ElMessage.warning('账号需登录或验证，已保存证据')
    else ElMessage.error(data.failure_reason || '自动填草稿失败')
    await refreshAll()
  } finally {
    tip.close()
    busy.runPublishTask = null
  }
}

const waitPublishTask = async (taskId) => {
  for (let i = 0; i < 45; i += 1) {
    await new Promise(resolve => setTimeout(resolve, 2000))
    const task = await apiJson(`/api/operations/publish/tasks/${taskId}`)
    if (task.status !== 'running') return task
  }
  throw new Error('自动填草稿超过90秒未完成，请检查平台弹框或验证')
}

const artifactUrl = (path, precomputed) => evidenceUrl(path, precomputed)

const createReport = async () => {
  const data = await apiJson(`/api/operations/reports/scorecard?brand_id=${brandId.value}`, { method: 'POST' })
  ElMessage.success(`成绩单已生成：${data.verified_rate}%`)
  await refreshAll()
}

const statusText = (status) => ({
  draft: '待审核',
  approved: '已审核',
  queued: '已排队',
  publishing: '发布中',
  published: '已发布',
  rejected: '已驳回',
}[status] || status || '未知')

const contentStatusType = (status) => ({
  draft: 'info',
  approved: 'warning',
  queued: 'primary',
  publishing: 'primary',
  published: 'success',
  rejected: 'danger',
}[status] || 'info')

const publishStatusText = publishStatusLabel

const publishStatusType = (status) => ({
  pending: 'info',
  assets_ready: 'info',
  prepared: 'warning',
  running: 'primary',
  manual_required: 'warning',
  login_required: 'warning',
  failed: 'danger',
  published: 'success',
  logged_in: 'success',
  browser_open: 'warning',
  not_connected: 'info',
  port_conflict: 'danger',
  port_conflict_not_closed: 'danger',
  closed: 'info',
  port_reserved: 'danger',
}[status] || 'info')

const closureStatusText = (status) => ({
  verified_loop: '已闭环',
  material_ready: '三件套已备',
  historical_failed: '历史失败',
  in_progress: '处理中',
}[status] || status || '未知')

const closureStatusType = (status) => ({
  verified_loop: 'success',
  material_ready: 'warning',
  historical_failed: 'info',
  in_progress: 'primary',
}[status] || 'info')

const workflowStatusText = (status) => ({
  template: '待测试',
  tested: '已测试',
  active: '启用',
  disabled: '禁用',
  draft: '未通过测试',
}[status] || status || '未知')
const workflowStatusType = (status) => ({
  template: 'warning',
  tested: 'success',
  active: 'success',
  disabled: 'info',
  draft: 'danger',
}[status] || 'info')

const capabilityText = (level) => executionLevelLabel(level)
const capabilityType = (level) => ({
  verified_loop: 'success',
  rpa_testing: 'warning',
  rpa_draft: 'warning',
  template_only: 'info',
  unknown: 'info',
}[level] || 'info')

const strategyStatusType = (status) => ({
  draft: 'warning',
  confirmed: 'success',
  archived: 'info',
}[status] || 'info')

const platformDecisionTagType = (level) => ({
  verified_loop: 'success',
  workflow_tested: 'success',
  draft_ready: 'warning',
  candidate: 'info',
  source_only: 'info',
}[level] || 'info')

const strategySectionItems = (value) => {
  if (Array.isArray(value)) return value.map(item => typeof item === 'string' ? item : JSON.stringify(item)).filter(Boolean)
  if (value) return [String(value)]
  return []
}

const actionTypeText = (type) => ({
  generate_keywords: '生成关键词',
  adopt_keywords: '采纳关键词',
  run_monitor: '运行监测',
  generate_content: '生成内容',
  create_publish_task: '创建发布任务',
  open_publish_browser: '打开发布浏览器',
  prepare_rpa_draft: '准备自动填草稿',
  run_recheck: '复测',
  generate_report: '生成报告',
}[type] || type)

const geoValueTagType = (type) => ({
  泛需求推荐词: 'success',
  场景推荐词: 'primary',
  品牌认知词: 'warning',
  售前问答词: 'info',
}[type] || 'info')

const getWorkflow = (platform) => workflows.value.find(item => item.platform === platform)
const publishGateReason = (row) => {
  if (row.status !== 'approved') return '内容未审核通过'
  const workflow = getWorkflow(row.target_platform)
  if (!workflow) return '发布平台未接入'
  if (row.target_platform === 'website') return '可建任务'
  if (['template_only', 'unknown'].includes(workflow.capability?.level)) return '平台仅模板，未完成发布闭环'
  if (!['tested', 'active'].includes(workflow.status)) {
    return workflow.notes || `工作流${workflowStatusText(workflow.status)}，不能发布`
  }
  if (!workflow.publish_url) return '缺少固定发布链接'
  const account = accounts.value.find(x => x.platform === row.target_platform)
  if (!account) return '未绑定账号'
  // 知乎专栏/百家号发布强制要封面图，缺图时提前亮出来，别等 RPA 跑一半才失败
  if (COVER_REQUIRED_PLATFORMS.includes(row.target_platform) && coverImageCount(row) === 0) {
    return '缺封面图：在「图片/视频」列点「选文件」上传'
  }
  return '可建任务'
}
const workflowSelectorReady = (row) => {
  if (row.platform === 'website') return true
  const selectors = row.workflow?.selectors || {}
  return !!(selectors.title?.length && selectors.body?.length)
}
const workflowTestGateReason = (row) => {
  if (row.platform === 'website') return '可测试'
  if (!row.publish_url) return '缺少固定发布链接'
  if (!workflowSelectorReady(row)) return '需先在自动发布工作流中固化标题/正文选择器'
  return '可测试'
}
const canTestWorkflow = (row) => workflowTestGateReason(row) === '可测试'
const canCreatePublish = (row) => {
  if (row.status !== 'approved') return false
  const workflow = getWorkflow(row.target_platform)
  if (!workflow) return false
  if (row.target_platform === 'website') return true
  if (['template_only', 'unknown'].includes(workflow.capability?.level)) return false
  const account = accounts.value.find(x => x.platform === row.target_platform)
  if (!['tested', 'active'].includes(workflow.status) || !workflow.publish_url || !account) return false
  return !COVER_REQUIRED_PLATFORMS.includes(row.target_platform) || coverImageCount(row) > 0
}
const canPrepareTask = (row) => ['pending', 'assets_ready', 'failed', 'manual_required', 'login_required'].includes(row.status)
const canRunTask = (row) => ['pending', 'assets_ready', 'prepared', 'failed', 'manual_required', 'login_required', 'running'].includes(row.status)
const canConfirmTask = (row) => ['prepared', 'manual_required', 'login_required', 'running'].includes(row.status)
const canAutoVerifyWebsite = (row) => row.platform === 'website' && ['pending', 'assets_ready', 'prepared', 'manual_required', 'failed'].includes(row.status)
const canRecoverPublishUrl = (row) => ['baijiahao', 'ctrip_travel', 'zhihu', 'toutiao', 'sohu'].includes(row.platform) && ['prepared', 'manual_required', 'login_required', 'running', 'published'].includes(row.status)
const canRetestTask = (row) => row.status === 'published' && !!row.published_url
const hasMaterialEvidence = (row) => !!(row.raw_artifact_verified && row.raw_artifact_path && row.screenshot_verified && row.screenshot_path && row.summary_artifact_verified && row.summary_artifact_path)

onMounted(async () => {
  brands.value = await apiJson('/api/operations/brands')
  const savedBrandId = Number(localStorage.getItem('geo_ops_brand_id'))
  const savedBrand = brands.value.find(brand => brand.id === savedBrandId)
  const dataRichBrand = brands.value[0]
  brandId.value = savedBrand?.id || dataRichBrand?.id || brands.value[0]?.id || null
  await refreshAll()
})
</script>

<style scoped>
.ops { padding: 20px; }
.ops-head, .toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
.keyword-cell { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; line-height: 1.45; }
.sub-toolbar { display: flex; gap: 10px; align-items: center; margin-top: 14px; color: #475569; font-size: 13px; }
.workbench { display: grid; grid-template-columns: repeat(4, 150px) 1fr; gap: 12px; align-items: stretch; margin-bottom: 16px; }
.metric { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 6px; padding: 12px; }
.metric span { display: block; color: #64748b; font-size: 12px; margin-bottom: 6px; }
.metric b { font-size: 24px; color: #1f2937; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.quality-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; padding: 10px 12px; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 6px; color: #64748b; font-size: 13px; }
.quality-sections { display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 10px; margin-bottom: 12px; }
.quality-section { background: #fff; border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px; font-size: 12px; color: #475569; }
.quality-section-head { display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-bottom: 6px; color: #111827; }
.quality-section p { margin: 0 0 6px; line-height: 1.5; }
.quality-section ul { margin: 6px 0 0; padding-left: 18px; line-height: 1.55; }
.quality-section .suggestions { color: #0369a1; }
.form-hint { margin-left: 10px; color: #64748b; font-size: 12px; }
.preview-body { white-space: pre-wrap; line-height: 1.7; background: #f6f8fa; padding: 12px; border-radius: 6px; max-height: 520px; overflow: auto; }
.strategy-layout { display: grid; grid-template-columns: minmax(360px, 0.9fr) minmax(520px, 1.1fr); gap: 16px; align-items: start; }
.strategy-main, .strategy-actions { border: 1px solid #e5e7eb; border-radius: 6px; padding: 14px; background: #fff; }
.strategy-title { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 12px; }
.strategy-title h3 { margin: 0 0 6px; font-size: 18px; color: #111827; }
.strategy-title p { margin: 0; color: #475569; line-height: 1.6; }
.strategy-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.strategy-block { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px; min-height: 82px; }
.strategy-block b, .gap-box b { display: block; margin-bottom: 6px; color: #111827; }
.strategy-block span { color: #475569; font-size: 13px; line-height: 1.6; }
.gap-box { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; padding: 10px; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; }
.strategy-meta, .muted { color: #64748b; font-size: 12px; }
.strategy-meta { margin-top: 10px; }
.warning-hint { color: #b45309; font-size: 12px; }
.knowledge-layout { display: grid; grid-template-columns: minmax(520px, 1.35fr) minmax(360px, 0.65fr); gap: 16px; align-items: start; }
.knowledge-panel { border: 1px solid #e5e7eb; border-radius: 6px; padding: 14px; background: #fff; }
.knowledge-title { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 12px; }
.knowledge-title h3 { margin: 0 0 6px; font-size: 17px; color: #111827; }
.knowledge-title p { margin: 0; color: #64748b; font-size: 13px; line-height: 1.55; }
.knowledge-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
.knowledge-stat { min-height: 76px; padding: 10px; border: 1px solid #e5e7eb; border-radius: 6px; background: #f8fafc; }
.knowledge-stat span { display: block; color: #64748b; font-size: 12px; margin-bottom: 4px; }
.knowledge-stat b { display: block; color: #111827; font-size: 22px; margin-bottom: 2px; }
.knowledge-stat small { display: block; color: #94a3b8; word-break: break-all; line-height: 1.35; }
.knowledge-actions { display: flex; align-items: center; gap: 10px; justify-content: flex-end; }
.execution-summary { margin: 12px 0; padding: 12px; border: 1px solid #fde68a; border-radius: 6px; background: #fffbeb; color: #92400e; font-size: 13px; }
.execution-title { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; color: #78350f; }
.failure-list { display: grid; gap: 6px; margin-bottom: 8px; }
.failure-item { display: grid; grid-template-columns: minmax(160px, 0.7fr) minmax(220px, 1.3fr); gap: 8px; padding: 7px 8px; border: 1px solid #fde68a; border-radius: 4px; background: #fff7ed; }
.failure-item span, .next-step { line-height: 1.5; }
.table-actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.table-actions :deep(.el-button) { margin-left: 0; }
@media (max-width: 1100px) { .workbench { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 1100px) { .strategy-layout { grid-template-columns: 1fr; } }
@media (max-width: 1100px) { .knowledge-layout, .knowledge-stats { grid-template-columns: 1fr; } }
@media (max-width: 1100px) { .quality-sections { grid-template-columns: 1fr; } }
</style>
