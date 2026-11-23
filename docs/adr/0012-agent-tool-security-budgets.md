# ADR-0012：Agent 工具最小权限、不可信证据与预算治理

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：5

## 背景

专业 Agent 需要查询企业 RAG、已采集网页来源和外部搜索。RAG 文档与网页内容都可能包含
恶意提示注入、虚假引用或越权诱导。模型输出不可信，不能直接变成正式 finding，也不能
让模型调用任意写接口、SQL、HTTP 或管理工具。企业还要求每个 run 有 token/成本/时长/
轮数/工具调用预算，避免成本失控。

## 决策

1. **只读工具白名单**：仅注册 `knowledge_search`、`source_search`、`source_read`、
   `web_search`。不向 Agent 暴露采集入队、来源删除、文档删除、成员/角色修改、任意
   HTTP、SQL、shell 或代码执行工具。工具注册在服务端代码中，模型无法新增工具。
2. **按当前 DB 权限重新授权**：worker 根据 run 的 `requested_by` 从 PostgreSQL 重新
   加载 active tenant/user/membership，并重新检查 `research_run:control`。RAG 使用
   `DocumentAccessScope`；外部来源要求真实 workspace/org 角色；web search 要求
   workspace editor+。JWT 角色/permission claim 永不参与。
3. **证据集合最小化**：知识库工具只返回通过 RAG ACL 回查的 chunk；来源工具只查询当前
   workspace 的 active WebSource/latest Snapshot；搜索工具只能返回已采集来源作为证据，
   未采集 URL 仅作为 lead。工具输出截断为有界长度。
4. **模型输出不可信**：模型只能返回 schema 化的 claim/evidence_id/evidence_quote/
   scope/limitations/confidence；服务端要求 evidence_id 存在于本次授权证据 ledger，
   且 evidence_quote 规范化后必须出现在原文中，否则丢弃并记录 `invalid_source_id` 或
   `unsupported_quote`。finding 的 source_id 由服务端从不信任的 evidence_id 映射，
   不由模型直接决定。
5. **提示注入按数据处理**：系统提示明确外部内容是 data；工具固定、服务端授权与引用
   校验才是安全边界。对常见注入语句做启发式标记 `injection_suspected`，供阶段 6
   Reviewer 使用，但不把该启发式当作唯一防护。
6. **预算分片与硬停止**：run 保存 server-side 上限；Supervisor plan、并行专业 Agent、
   模型调用和工具调用都按剩余预算切片。任一类预算耗尽时停止并持久化为
   `budget_exhausted`；模型返回的 token usage 计入 run，成本由服务端按配置单价计算，
   不信任模型返回的成本。
7. **有界重试**：模型 HTTP 网关只对网络/429/5xx 做有限指数重试；单 task 失败后由
   Supervisor 在 `max_task_attempts` 和剩余预算内重试；不存在无限互聊或无限补查。
8. **模型网关边界**（契约见 [`agent-model-gateway.md`](../agent-model-gateway.md)）：`ResearchModel` 抽象隔离供应商；`deterministic` 只用于开发/测试，
   production 强制 `http` + https URL + Secret Manager 密钥。网关只接收当前 tenant/
   workspace/project 授权范围内的证据，部署方负责供应商数据保留/跨境/PII 治理。
9. **可追溯性**：每次工具调用写入 `research_tool_calls`（工具、agent、query 或 source
   id、状态、结果数、延迟、error_code），不记录原始网页正文、token 或密钥；finding、
   checkpoint 和审计均租户隔离。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 固定只读工具 + 服务端引用校验 | 攻击面小、可验证 | 自主性受限 | 采纳 |
| 让模型自由调用工具 | 灵活 | 提示注入/越权/成本不可控 | 拒绝 |
| 信任模型返回 source_id | 简单 | 可伪造引用/越权来源 | 拒绝 |
| 只靠提示词防注入 | 低成本 | 不可作为安全边界 | 不足 |
| 每 run 只设全局预算 | 简单 | 并行分支可超支 | 不足 |
| 预算按任务切片 | 严格 | 小任务可能提前停止 | 采纳 |

## 后果

- 正面：Agent 无法直接写数据或访问任意网络；伪造引用被拒绝；并行预算可控；所有工具
  调用和 checkpoint 可审计回放。
- 负面：固定的工具流程不如开放式 Agent 灵活；来源搜索仍是 ILIKE 候选；模型网关是
  数据出域点；提示注入标记是启发式而非完整检测。
- 安全：worker 仍应部署在受控 egress；模型网关 URL 由运维配置；数据库生产角色必须
  非超级用户以让 RLS 生效。
- 后续：阶段 6 增加证据存在性/相关性/独立性/新鲜度审查和冲突补查；阶段 8/9 增加
  租户配额、成本归集、PII 脱敏和模型供应商治理。

## 验证

单元测试覆盖只读工具注册、非法引用拒绝、恶意来源注入标记、并行预算耗尽、轮数上限、
失败 task 重试、HTTP 网关非法角色/超大响应/瞬时重试和不可信证据 payload。集成测试
覆盖 worker 重新授权、source-bound finding、只允许 source_search、跨租户 404、运行中
暂停/恢复不重复工具调用、预算耗尽持久化及 RLS。
