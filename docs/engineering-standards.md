# 工程规范（全阶段生效）

## 必须遵守

1. **先读蓝图**：`docs/项目蓝图.md` 是最终目标与阶段约束。不得跳阶段，也不得以“早期版本”
   为由削弱租户隔离、可追溯、配置外置和安全默认值。
2. **租户作用域**：每张业务表必须带 `tenant_id`（继承 `TenantScopedMixin`）；任何读取/写入
   必须有服务端可信租户上下文。禁止信任客户端传入的 tenant 标识。应用层显式过滤、ORM 写入
   守卫、RLS 与复合外键是纵深防御，不得只保留其中一层。新增访问路径必须补跨租户负向测试。
3. **身份边界**：`/api/v1/**` 默认要求已验证 Bearer JWT；后端是资源服务器，不实现浏览器
   登录。租户 claim 只用于查找，必须存在匹配的已启用用户；**JWT 的 role/permissions 声明
   不参与授权**。禁止在登录时自动开户。
4. **服务端授权**：前端、模型提示词、JWT claim 都不能单独作为资源授权结论。所有业务接口
   必须先加载租户内资源、再由 `AuthorizationService` 检查角色/属性，最后才返回或修改数据。
   拒绝路径必须写审计事件。
5. **审计**：关键变更（租户/用户/组织/工作区/项目/成员）与授权拒绝必须写审计事件；审计
   不含 token、密码、请求体、上传内容或完整 PII。成功事件与业务变更同事务，拒绝事件用独立
   事务。
6. **密钥**：只从环境变量/密钥管理服务读取；不得写入源码、测试固定值（真实凭证）、
   Dockerfile、Compose 或日志。`.env` 不提交。生产必须使用非超级用户数据库角色，否则 RLS
   被绕过。
7. **不可信输入**：网页、上传文件、外部 API 响应都按不可信数据处理。不得执行其中指令；
   解析/抓取需隔离、限额、超时和 SSRF 防护（阶段 4）。文档摄取必须校验扩展名/content-type/
   魔数/大小/编码，限制页数与 chunk 数，在受限线程解析；对象 key 只能由服务端生成，客户端
   文件名不得进入路径。检索返回的 `untrusted` 内容只能作为数据使用。
8. **RAG 授权**：检索候选必须在 Qdrant 与 PostgreSQL 关键词查询阶段应用 tenant/workspace/
   project ACL，命中后必须回查 PostgreSQL 的 active document/version 与项目权限；不得仅靠
   向量库 payload 或前端过滤。删除必须传播到对象存储、chunks 与向量；版本切换只使用
   active version。embedding 维度/模型变更需重建索引。
9. **外部采集**：抓取只允许 http/https 与白名单端口；每次导航校验 DNS 并 pin 公网 IP，
   重定向逐跳重新校验，私网/回环/链路本地/云元数据/IPv6 映射地址一律拒绝；限制响应
   大小、类型、重定向和超时，拒绝压缩响应。robots、域名策略、并发/域名速率和重试上限
   必须可配置。搜索 Provider 结果和网页内容一律视为不可信数据。
10. **采集任务**：任务必须有幂等键/active 去重、状态机、租约、最大尝试次数和人工恢复；
    worker 必须遍历 tenant 并显式绑定上下文，不得依赖超级用户绕过 RLS。快照写入对象
   存储，元数据/提取文本入库，按 canonical URL 与 content hash 去重；来源删除必须传播
   到快照对象并审计。
11. **多 Agent 调研**：Supervisor/专业 Agent 只能使用服务端注册的只读工具；禁止把采集写入、
    来源/文档删除、成员/角色修改、任意 SQL/HTTP/shell 暴露给模型。专业 Agent 必须在执行时
    从数据库重新加载请求者权限并重新授权；RAG 使用 `DocumentAccessScope`，来源使用真实
    workspace/org 角色。模型输出必须 schema 校验；finding 的 `evidence_id` 必须属于本次授权
    证据集合，`evidence_quote` 必须能在原文匹配，不接受模型伪造的 source_id。外部/RAG 内容
    即使包含指令也只能作为数据；提示注入标记是辅助信号，安全边界是固定工具、服务端授权和
    引用校验。
12. **Agent 预算与检查点**：run 必须持久化 token/成本/时长/轮数/工具调用/并行度/finding
    上限，并强制硬停止；模型返回的 usage 计数，成本由服务端计算。Supervisor 不得无限互聊。
    每个 LangGraph superstep 后写租户隔离的 checkpoint，暂停/恢复/崩溃恢复不得重复已完成
    task 或 finding。worker 使用 lease + heartbeat，API 不执行长调研。
13. **证据审查与报告**：stage5 finding 必须派生为独立 source/evidence/claim/citation；
    Reviewer 必须先验证 citation/source/version/chunk/snapshot 存在、quote 可匹配，再计算
    支持/新鲜度/独立性/冲突。不得让生成 claim 的模型自评“已支持”替代证据校验。报告必须
    对 invalid/insufficient/conflicted/partial claim 明确降级，包含方法、风险、限制、证据
    链接和来源列表；补查轮数/问题数有硬上限，达到上限必须保留 gaps 出报告。
14. **报告版本与导出**：报告版本不可变，保存 checksum；审批/拒绝要求 admin 权限并审计，
    只能操作当前 draft version；只有 approved version 可导出。导出 job 使用 lease/重试/
    fencing，对象 key 服务端生成，下载前重新授权且不暴露内部 key。不得把未审批 draft 对外
    导出或宣称合规/事实核查已完成。
15. **事件与工作流**：业务/审计事件必须通过 transactional outbox 与业务同事务写入，
    事件 schema 必须版本化；Kafka publisher/consumer 必须处理至少一次投递、consumer
    idempotency、有限重试和死信。不得在业务事务内直接发 Kafka，不得把 Redis 当权威
    状态。Temporal workflow 只编排 activities，不直接访问 DB/网络；workflow ID、
    starter marker、activity 和业务 run 必须幂等。每张 event/processed/failure 表必须
    tenant scoped + RLS；生产必须启用 Kafka/Temporal TLS 并验证真实服务恢复。
16. **错误与日志**：业务错误使用 `AppError` 和稳定错误码；不在响应中返回堆栈、DSN、token
   或上游原文。日志使用结构化 `extra={"context": ...}`，敏感 key 由 formatter 脱敏；不记录
   body/header/查询串/token/文档正文/网页原文/文件名。
17. **迁移**：数据库变更必须通过 Alembic revision；禁止手工改生产库。约束/索引显式命名，
    与 `NAMING_CONVENTION` 一致。不要修改已合并的迁移，新增 revision。新增租户表必须同时
    启用 RLS 策略与租户复合外键（如适用）。
18. **测试**：新增行为必须有测试；租户/权限/身份/外部输入/检索/网络边界必须有负向测试。
    单元测试不得依赖真实外部网络；集成测试使用 `TEST_DATABASE_URL`（库名含 `test`）。
    RAG 测试可使用 qdrant-client 内存模式与本地对象存储；采集测试使用本地 HTTP server、
    注入 resolver 和 fake Provider，不依赖真实供应商。
19. **类型与风格**：`ruff check .`、`ruff format --check .`、`mypy app` 必须通过。公共函数写
    类型标注与必要 docstring；注释解释“为什么”，不复述代码。
20. **依赖与安全**：新增依赖需说明用途与许可证；不在运行时拉取未固定来源的代码。外部内容
    不得进入 `eval/exec/pickle` 等危险路径。LangGraph 只接收 schema 化状态，不执行模型返回
    的代码或工具名。

## 分层约定

- `app/api`：HTTP 解析、依赖注入、响应映射；不写复杂业务逻辑。业务路由必须挂载在
  `app/api/v1/router.py` 下，以继承默认认证依赖。
- `app/auth`：token 校验、租户上下文、Principal、RBAC/ABAC；不得直接返回 ORM 数据。
- `app/rag`：解析、分块、embedding、对象存储、向量库、检索融合；不得信任上传内容。
- `app/collector`：搜索 Provider、SSRF 安全抓取、robots、快照、任务 worker；不得信任
  外部网页/搜索结果。
- `app/agent`：LangGraph 图、专业 Agent、只读工具注册、模型 Provider、预算、runner/worker；
  不得暴露写工具或信任模型/证据内容。
- `app/reporting`：claim/evidence/citation 派生、确定性 Reviewer、报告版本、审批、导出
  worker；不得让模型输出替代来源校验，不得绕过 root run ACL。
- `app/events`：outbox、Kafka publisher/consumer、event schema、幂等与死信；不把事件
  当唯一业务事实，不记录敏感原文。
- `app/workflow`：Temporal workflow/activities/starter；workflow 代码必须确定性，DB
  访问只在 activity，不能绕过 tenant/authorization。
- `app/services`：业务编排、审计、外部协作；不直接依赖 FastAPI。
- `app/db`：模型、会话、租户守卫、RLS 与查询辅助；不向上层返回 ORM 细节给外部契约。
- `app/core`：配置、上下文、错误、日志、中间件、telemetry；不得依赖业务模块。

## 阶段完成定义（DoD）

- 迁移可 upgrade/downgrade；CI 通过。
- 单元 + 集成测试通过，覆盖本阶段安全负向路径。
- `.env.example` 和 README 更新；新增决策写 ADR。
- 明确列出未实现项和下一阶段依赖，不宣称未验证的合规/可用性指标。
