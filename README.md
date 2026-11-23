# 企业级多角色市场调研 Agent — 后端

面向企业团队的市场情报与调研平台后端。最终架构与交付阶段见
[`docs/项目蓝图.md`](docs/项目蓝图.md)。

> **当前实现范围：阶段 8「企业监测、协作与租户配额」。**
> 已完成：阶段 1 工程底座、阶段 2 身份/租户/RBAC/ABAC/审计、阶段 3 资料摄取/RAG、
> 阶段 4 搜索/安全采集、阶段 5 LangGraph Supervisor/专业 Agent、阶段 6 证据审查/
> 报告/审批/导出、阶段 7 的 transactional outbox/版本化事件/Kafka/Temporal，以及
> 阶段 8 的监测订阅与调度、内容变更检测、加密通知渠道、安全 webhook 投递、租户
> 配额、项目评论和协作审批节点。详见
> [`docs/monitoring-and-collaboration.md`](docs/monitoring-and-collaboration.md)。
> 尚未实现：生产运营治理（阶段 9）等。

## 技术栈

- Python 3.12、FastAPI、Pydantic v2 / pydantic-settings
- SQLAlchemy 2（asyncpg）、Alembic、PostgreSQL 16、RLS、全文检索
- PyJWT（非对称签名校验）、Email Validator
- S3 兼容对象存储（本地 MinIO / 生产 S3）、boto3
- Qdrant（qdrant-client）、可替换 embedding provider、pypdf
- 可配置外部搜索 Provider、隔离采集 worker（SSRF/DNS pinning/robots）
- LangGraph 1.x（MIT；Supervisor + 专业 Agent、并行 fan-out、应用级持久检查点）
- 报告导出：Markdown + DOCX（python-docx，MIT）；PDF/表格导出尚未实现
- Transactional outbox、Kafka（aiokafka）、Temporal Python SDK
- OpenTelemetry（可选，OTLP/HTTP）
- pytest / pytest-asyncio、Ruff、mypy（strict）、GitHub Actions
- Docker / Docker Compose

## 目录结构

```text
app/
  main.py              # uvicorn 入口
  factory.py           # create_app：中间件、生命周期、路由装配
  cli.py               # 平台运维 Bootstrap（创建租户/首个租户管理员）
  api/                 # 健康探针、统一错误、v1 业务路由
  auth/                # OIDC/JWT 校验、租户上下文中间件、Principal、RBAC/ABAC
  core/                # 配置、上下文、错误、日志、HTTP 中间件、trace
  db/                  # Base、模型、租户守卫、RLS、引擎
  rag/                 # 解析、分块、embedding、对象存储、Qdrant、混合检索
  collector/           # 搜索 Provider、SSRF 安全抓取、robots、worker、来源/快照
  agent/               # LangGraph Supervisor、专业 Agent、只读工具、预算、runner/worker
  reporting/           # Reviewer、claim/evidence/citation、报告生成、审批、导出/worker
  events/              # outbox dispatcher、Kafka publisher/consumer、死信与事件管理
  workflow/            # Temporal workflow、activities、starter、worker 模式
  monitoring/          # 监测调度、变更检测、加密通知渠道、SSRF 安全 webhook
  collaboration/       # 项目评论与协作审批节点
  governance/          # 租户配额与 advisory lock 强制
  schemas/             # API schema
  services/            # 租户/成员/用户/审计/文档/RAG 编排
alembic/               # 迁移
tests/unit/            # 无需 PostgreSQL（含内存 Qdrant、解析/分块/embedding）
tests/integration/     # 真实 PostgreSQL：API、RLS、迁移、CLI、RAG
docs/adr/              # 架构决策记录
docs/engineering-standards.md
docker/postgres/init/  # 本地测试库初始化
```

## 快速开始

### 1. 配置

```bash
cp .env.example .env
# 至少修改 POSTGRES_PASSWORD、MINIO_ROOT_PASSWORD，并保证 DATABASE_URL 中的密码一致。
# 本地默认启用 RAG、MinIO（S3 兼容）+ hash embedding；生产必须改为真实 S3/HTTP embedding。
# 如需监测/webhook，设置 MONITORING_ENABLED=true 并注入 Fernet 格式的
# NOTIFICATION_ENCRYPTION_KEY（见 docs/monitoring-and-collaboration.md）。
```

生产/预发必须由密钥管理服务或编排平台注入环境变量；`.env` 仅用于本地开发且已被
`.gitignore` 忽略。代码中没有任何密钥默认值。

### 2. 启动依赖

```bash
docker compose up -d postgres qdrant minio
```

Compose 只把端口绑定到 `127.0.0.1`。Postgres 首次初始化时会额外创建
`market_intel_test`（见 `docker/postgres/init/`）。MinIO 控制台为
`http://127.0.0.1:9001`，API 启动时会自动创建 `S3_BUCKET`（仅本地
`S3_AUTO_CREATE_BUCKET=true`；production 禁止）。若卷已存在，需手工创建或
`docker compose down -v` 后重建（会删除本地数据）。

某些网络对官方 MinIO 镜像的匿名拉取有限制；不可用时可将
`OBJECT_STORAGE_BACKEND=local` 并把 `OBJECT_STORAGE_LOCAL_PATH` 指向持久卷，
应用仍可按同一接口运行（生产仍必须使用 S3）。

### 3. 安装、迁移、运行

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log

# 另开进程运行采集 worker（COLLECTOR_ENABLED=true 时）
python -m app.collector.worker

# 另开进程运行研究/审查/导出 worker（AGENT_ENABLED=true 时）
python -m app.agent.worker

# 另开进程运行 outbox dispatcher / Kafka consumer（可选 consumer）
python -m app.events.worker

# 另开进程运行监测调度/变更检测/通知投递（MONITORING_ENABLED=true 时）
python -m app.monitoring.worker
```

`agent` worker 有两种模式：`TEMPORAL_ENABLED=false` 时使用 PostgreSQL 队列；true 时
启动 Temporal Worker + outbox starter，并保留 review/export 安全循环。
`events` worker 负责将 transactional outbox 派发到 Kafka（开发可 `log`），并按
consumer group 做幂等消费和死信记录。

`monitor` worker 使用 PostgreSQL lease 队列调度监测订阅：搜索 -> 复用采集器抓取 ->
按快照内容哈希检测新来源/内容变化 -> 按告警规则创建通知投递。webhook 渠道 URL 和
签名密钥使用 `NOTIFICATION_ENCRYPTION_KEY` 加密存储，发送时复用采集器的 SSRF/DNS
固定策略，不跟随重定向。租户配额（研究运行、采集任务、监测订阅、并发监测运行、
通知投递量）通过 PostgreSQL advisory transaction lock 强制执行；监测尚未配置搜索
Provider 时运行会安全失败，而不是编造来源。

或使用 Makefile（`make install-dev`、`make migrate`、`make run`）。
Docker Compose 会同时启动 `collector`、`agent`、`events`、`monitor` worker，以及本地
Redpanda/Temporal；worker 都没有入站端口。`.env.example` 默认使用 log publisher 和
DB queue，不要求 Kafka/Temporal 即可启动；设置 `EVENT_PUBLISHER=kafka`、
`EVENT_CONSUMER_ENABLED=true`、`TEMPORAL_ENABLED=true` 后切换到真实事件/工作流模式。
`.env.example` 默认使用 `AGENT_PROVIDER=deterministic` 的本地确定性模型契约桩，
只用于开发和接口验证；生产必须配置 `AGENT_PROVIDER=http` 与受控 https 模型网关。

### 4. 验证

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

- `/health/live`：进程存活，不访问外部依赖。
- `/health/ready`：检查 PostgreSQL；配置了 `QDRANT_URL` 时同时检查 Qdrant。
- 开发环境默认开放 `/docs`、`/redoc`、`/openapi.json`；production 默认关闭。
- **所有 `/api/v1/**` 路由默认要求 OIDC Bearer Token。** 未配置 OIDC 时 API 会
  fail closed 返回 401；健康探针仍可用。

### Docker 全量运行

```bash
cp .env.example .env   # 修改 POSTGRES_PASSWORD，配置 OIDC_*
docker compose up -d --build
curl http://127.0.0.1:8000/health/ready
```

Compose 中的一次性 `migrate` 服务在 API 启动前执行 `alembic upgrade head`。
镜像以非 root 用户运行；不在构建阶段写入任何密钥。

## 身份与 OIDC 边界

后端是 **资源服务器**：浏览器登录由前端/身份网关完成，后端只验证 Bearer JWT。

- 校验签名、`iss`、`aud`、`exp`/`iat`；仅允许配置的非对称算法（RS/ES/PS），
  拒绝 `HS*` 与 `none`。
- 必需的 claim：`iss`、`aud`、`sub`、`exp`、`iat`，以及配置的
  `OIDC_TENANT_CLAIM`（默认 `tenant_id`，UUID）。
- **租户 claim 只是查找键**：服务端必须在该租户中存在与 `(iss, sub)` 匹配的
  已启用用户记录，并从 PostgreSQL 加载其成员角色。JWT 中的 `role`、`roles`、
  `permissions` 等声明一律不参与授权。
- **不做自动开户**：用户必须由租户管理员或 Bootstrap CLI 预先创建。
- SAML 暂不解析 XML；建议通过企业身份网关转换为 OIDC/JWT，或后续实现同一
  `TokenVerifier` 边界。

初次部署（没有租户管理员时可执行）：

```bash
market-intel create-tenant --slug acme --name "Acme"
market-intel provision-user \
  --tenant-slug acme \
  --subject "<idp-sub>" \
  --email admin@example.com \
  --display-name "Tenant Admin" \
  --tenant-role tenant_admin
# 等价：python -m app.cli ...
```

随后由租户管理员通过受审计的 API 管理用户；CLI 与 API 写入都会产生审计事件。

## RBAC / ABAC

角色按作用域存储，父级角色向子级继承，子级角色不向上授予写权限。

| 作用域 | 角色 | 能力 |
| --- | --- | --- |
| Tenant | `tenant_admin` | 创建组织、管理用户、读取审计、租户内全部资源 |
| Tenant | `member` | 无隐式资源权限，必须被授予作用域角色 |
| Organization | `admin` / `editor` / `viewer` | 管理成员 / 创建/更新工作区 / 只读 |
| Workspace | `admin` / `editor` / `viewer` | 管理成员 / 创建/更新项目 / 只读 |
| Project | `admin` / `editor` / `viewer` | 管理成员 / 更新项目 / 只读 |

- API 请求先加载资源（租户过滤，跨租户返回 404），再由
  `AuthorizationService` 判断权限；无权限返回 403 并写入拒绝审计。
- 组织必须保留至少一个 active `admin`；撤销/降级最后一个管理员返回 409。
- ABAC 限制：组织 suspended、工作区 archived、项目 archived 时，非 admin 写操作
  被拒绝；`confidential` 项目的写操作要求 admin 角色；工作区/项目状态与项目密级
  变更要求 admin；租户 suspended 时所有 API 请求被拒绝（平台运维可通过 CLI/数据库恢复）。

## 资料摄取与混合检索（RAG）

- **对象存储**：`ObjectStorage` 接口提供本地 S3/MinIO 与生产 S3 适配器。对象 key
  全部由服务端生成（tenant/workspace/document/version），客户端文件名只作为展示元数据，
  不参与路径。生产建议启用桶级加密、生命周期与删除策略。
- **上传**：仅允许 `.txt` / `.md` / `.markdown` / `.pdf`，校验扩展名、content-type、
  PDF 魔数、大小上限与 NUL/UTF-8；解析在受限线程中执行，限制 PDF 页数、解析字符数与
  chunk 数。上传内容、网页内容都视为不可信数据。
- **状态机**：版本从 `uploaded → processing → indexed/failed`，可重试 `failed`；
  新版本成功索引后旧版本标记 `superseded`；删除状态为
  `deleting → deleted/delete_failed`，可重试删除。
- **解析与分块**：PDF 保留页码；所有 chunk 保存页码、段落号、页内 offset、内容哈希、
  embedding model/version 和稳定 `chunk_id`，用于引用定位与幂等重建。
- **Embedding**：`EMBEDDING_PROVIDER=hash` 仅为本地开发/测试（确定性词法哈希）；
  production 必须使用 HTTP embedding endpoint，并要求 `EMBEDDING_DIMENSION` 与
  Qdrant collection 一致。检索只接受当前 `EMBEDDING_MODEL`/`EMBEDDING_VERSION` 的
  chunk；切换模型后旧索引默认不可见，必须重建。
- **Qdrant**：单一 collection + payload 过滤器；payload 含 tenant/workspace/project/
  document/version，检索前即在候选阶段按租户与文档 ACL 过滤。
- **混合检索**：PostgreSQL 全文检索 + 参数化 ILIKE 关键词回退 + Qdrant 向量，
  使用 Reciprocal Rank Fusion 合并并做轻量重排（精确短语/标题加分）。
- **权限回查**：候选命中后必须回 PostgreSQL 读取 chunk/document/version，确认
  document 仍 active、chunk 属于 active version、文档属于当前 workspace/project
  ACL；否则丢弃。未授权材料不会进入返回结果或模型上下文。
- **文档 ACL**：工作区级文档要求组织/工作区角色；项目级文档要求项目角色（父级角色
  向下继承）。仅有项目成员的用户看不到 workspace-wide 文档。
- **版本切换/删除传播**：索引成功后才切换 `documents.active_version_id`；删除文档
  会删除对象存储原文、Qdrant points、chunk 行并标记版本/文档 deleted，失败会进入
  `delete_failed` 并可重试。重新索引会先删除该版本旧 points/chunks，使用确定性 ID
  幂等重建。
- `RAG_ENABLED=false` 时文档/搜索路由 fail closed 返回 503；工作区/项目/身份接口不受影响。

## 外部搜索与安全采集

- **搜索 Provider**：`SearchProvider` 接口与通用 HTTP JSON contract 适配器；供应商
  凭证只从 `WEB_SEARCH_API_URL` / `WEB_SEARCH_API_KEY` 环境读取。搜索结果（URL、
  title、snippet）一律标记 `untrusted=true`，不会自动写入正式来源。
- **隔离 worker**：`python -m app.collector.worker` 作为无入站端口的独立进程运行；
  遍历 active tenant 并显式绑定租户上下文，再使用 `FOR UPDATE SKIP LOCKED` 认领任务，
  不依赖超级用户绕过 RLS。API 只负责授权、入队和读取状态。
- **SSRF / DNS 重绑定防护**：仅允许 http/https、80/443（可配置，production 限制为
  80/443），拒绝 userinfo、非白名单端口、IP literal 私网/回环/链路本地/云元数据地址
  和 IPv6 映射地址；每次导航都解析 DNS 并拒绝任何非公网答案（混合答案 fail closed），
  连接直接 pin 到已校验 IP，同时保留 Host/SNI 证书校验。跨域/私网重定向逐跳重新校验，
  有最大重定向和循环检测。
- **内容边界**：限制响应字节数、提取字符数、重定向层级和请求超时；只接受
  text/html、text/plain、application/xhtml+xml，拒绝压缩响应（要求 identity）；不执行
  JavaScript，HTML 抽取器忽略 script/style/noscript 等。
- **域名与 robots**：全局 allow/block 域名策略；请求前检查 robots.txt（缓存、支持
  `User-agent`/`Allow`/`Disallow`/`Crawl-delay`），401/403 拒绝、5xx 临时不可用、
  404 允许；无法获得 robots 时按安全策略处理。
- **并发与速率限制**：worker 全局并发上限；PostgreSQL 原子 upsert 的
  `collector_domain_limits` 为每个 tenant+domain 预留起始时隙（含 Crawl-delay），
  多 worker 共享，不依赖进程内状态。
- **任务状态机**：`pending → running → succeeded/retry/failed`；索引/删除式任务有
  租约与过期回收、最大尝试次数、指数退避和人工 retry API。worker 崩溃后过期 lease
  可被重新认领。
- **快照与去重**：原始响应写入对象存储（`tenants/{tenant}/workspaces/{workspace}/
  sources/{snapshot_id}`），元数据、提取文本和内容哈希写入 PostgreSQL；来源按
  workspace + canonical URL hash 去重，快照按 content hash 去重，相同内容不重复写对象。
- **来源 ACL**：来源与采集任务都是 workspace-wide 外部资料，读取要求真实组织/工作区
  角色（`workspace:content_read`，不接受项目成员的祖先只读回退）；任务可带
  `project_id` 作为过滤元数据，但项目成员不能单独访问来源库。来源删除要求 workspace
  admin，并删除快照对象/行、写入审计。
- 外部网页内容只作为数据；不执行其中指令。`COLLECTOR_ENABLED=false` 时相关 API
  fail closed 返回 503。

## 多 Agent 调研（LangGraph）

- **运行模型**：`POST /research-runs` 只做授权、持久化和入队；独立
  `python -m app.agent.worker` 通过 PostgreSQL lease + `FOR UPDATE SKIP LOCKED`
  认领 run，遍历 active tenant 并显式绑定租户上下文。worker 无入站端口，崩溃后
  过期 lease 可从最后一个检查点恢复。
- **Supervisor + 专业 Agent**：LangGraph `StateGraph` 的 Supervisor 制定计划，
  使用 `Send` 并行 fan-out 到 `industry`、`competitor`、`user` 专业 Agent；完成后
  聚合、判断是否在有界预算内补查，再 finalize。不存在开放式 Agent 互聊。
- **结构化发现**：每个 finding 包含 `claim`、证据原文 `evidence`、`source_id`、
  `agent_role`、`observed_at`、`scope`、`limitations`、`confidence` 和
  `evidence_untrusted=true`。文档来源保存 document/version/chunk 与页码/段落/offset，
  外部来源保存 WebSource/Snapshot；无法通过引用校验的 finding 会被丢弃。
- **工具最小权限**：只注册 `knowledge_search`、`source_search`、`source_read`、
  `web_search` 四个只读工具，没有采集写入、来源删除、成员/角色修改、任意 HTTP 或
  任意 SQL 工具。worker 在运行开始及每个 superstep 后按请求者当前 DB 角色重新授权，
  不使用 JWT 角色声明。
  RAG 工具只接收已授权 `DocumentAccessScope`；来源工具要求真实 workspace/org 角色；
  web search 额外要求 workspace editor+。仅有项目角色的用户可运行项目文档 RAG 调研，
  但不能使用 workspace-wide 外部来源库；工作区级 run 同样要求真实 workspace/org 角色，
  不接受项目成员的祖先只读回退。
- **不可信输入**：RAG chunk 与网页快照均标记为不可信数据。模型返回的
  `evidence_id` 必须存在于本次授权证据集合中，`evidence_quote` 必须能在原文中匹配；
  否则 finding 被拒绝。常见提示注入文本会被标记 `injection_suspected=true`，Reviewer
  和报告会保留该标记，但安全不依赖该启发式规则：工具集合固定、来源服务端校验、模型不能调用任意工具。
- **预算治理**：run 保存 token/成本/tool-call/时间/轮数/并行度/最大 finding 数等
  server-side 预算。plan、每个专业 Agent 和每次模型调用都受预算切片约束；并行任务
  按剩余预算均分，预算耗尽会持久化为 `budget_exhausted`，不会无限循环。
- **检查点与回放**：每个 LangGraph superstep 后将完整 JSON 状态写入
  `research_checkpoints`，同事务更新 run 的 usage、round、plan 和计数。暂停/恢复和
  worker 崩溃恢复都从最后检查点继续；`GET .../checkpoints` 返回可审计的回放状态。
  当前“回放”是状态/轨迹检查，不是对外部模型调用的确定性重放。未使用 LangGraph 原生
  checkpoint 表，因为它不满足本项目的 tenant/RLS 约束。
- **人工控制**：`pause`、`resume`、`cancel` API 均是服务端授权操作；运行中的 pause/
  cancel 在当前 superstep 完成后生效，并将状态持久化。恢复时跳过已完成 task，只继续
  未完成或可重试 task，避免重复 finding。每个 superstep 后重新加载请求者成员关系与
  document/tool scope；权限/能力变化或控制权丢失会把 run 标记为 failed，停止后续步骤。
- **模型网关**：`ResearchModel` 抽象提供 `deterministic`（本地契约测试桩）与通用
  `http` JSON contract。模型 URL/密钥仅来自环境/Secret Manager；production 强制
  `AGENT_PROVIDER=http` + https。run 记录创建时的 `model_provider`/`model_name`/
  `graph_version` 以便回放和审计；worker 会校验当前 provider/model/graph version 与
  run 创建时一致，不一致则安全失败，避免静默换模型。模型网关会收到租户授权范围内的
  证据文本，部署方必须将其纳入数据跨境/供应商评审；当前阶段未实现 PII 自动脱敏。
- **失败隔离**：单个专业 Agent 的工具/模型失败不会丢弃其他 Agent 的成功结果；
  task 在预算内有限重试，最终可标记 `partial`。provider/plan 失败、无授权工具、
  授权被撤销或无 finding 会明确落为 failed/budget_exhausted 状态。
- `AGENT_ENABLED=false` 时研究 API fail closed 返回 503；文档、采集和身份 API 不受影响。

## 证据审查与交付报告（阶段 6）

- **不可混淆的实体**：阶段 5 的 finding 会被规范化为独立的
  `research_sources`、`research_evidence`、`research_claims`、
  `research_citations`。source 表示文档版本或外部快照，evidence 表示可定位的
  引用片段，claim 表示待审查结论，citation 表示 claim→evidence 关系；均按 root run
  聚合，同一来源/证据/claim 幂等去重。
- **Reviewer 检查**：审查由确定性规则执行，不把模型自评当作证据：
  1. citation 必须存在，且对应 document/chunk/version 或 external snapshot 仍可读取；
  2. quote 必须仍能在来源文本中匹配；
  3. claim 与 evidence 必须有足够的词法支持分数；
  4. 报告证据超过 `REPORT_MAX_EVIDENCE_AGE_DAYS` 或文档版本已非 active 时标记 stale；
  5. 统计独立来源数；少于 `REPORT_MIN_INDEPENDENT_SOURCES` 时降级为 partial；
  6. 对共享显著词且极性相反的 claim 标记 conflict（保守词法启发式，不替代人工领域判断）。
- **状态与降级**：claim 状态为 `supported` / `partially_supported` / `insufficient` /
  `conflicted` / `invalid`。无有效引用的 claim 明确标为 invalid/insufficient；报告不会把
  无法验证的结论写成已支持结论。外部文本、引用和 injection 标记保持 `untrusted`。
- **有限补查**：Reviewer 对 invalid/insufficient/conflicted（以及独立来源不足的
  partial）生成最多 `REPORT_MAX_FOLLOWUP_QUESTIONS` 个补查问题，并创建
  `run_kind=followup`、`root_run_id` 指向 root run 的新研究 run；补查轮数受
  `REPORT_MAX_FOLLOWUP_ROUNDS` 硬限制。followup run 仍由 langGraph worker 执行，
  完成后自动唤醒 root review。达到上限后必须出报告并保留 gaps/conflicts，不会无限补查。
- **报告生成**：由结构化 claim/evidence/source 确定性生成 Markdown，包含执行摘要、
  方法、发现、风险与限制、冲突与缺口、证据链接、来源列表和审查汇总。支持
  supported/partial findings 与 downgraded gaps；报告内容有长度上限并记录截断。
- **不可变版本与完整性**：每次 review 生成新的 `report_versions` 版本，保存 Markdown、
  结构化 JSON、来源列表、citation index 和覆盖 Markdown+结构化 JSON 的 SHA-256
  checksum；旧版本保持不变。审批/导出前必须通过 checksum 校验。报告状态为 draft/approved/rejected。
- **人工审批**：`REPORT_APPROVE` 要求 workspace/project admin；只能审批当前 draft
  version，可拒绝并记录原因。审批/拒绝写入审计，报告版本号、checksum 与 actor 可追踪。
- **导出任务**：只有 approved version 可创建导出 job；worker 支持 Markdown 与 DOCX，
  写入 tenant/workspace/report/version 命名空间的对象存储。job 有 lease、最大尝试、
  指数退避、失败码和审计；下载 API 只返回已完成导出，使用服务端生成文件名并禁止缓存。
- **权限**：报告读取使用 `report:read`（workspace 级 run 不接受项目成员祖先回退）；
  review 启动/重跑使用 `research_run:control`；审批使用 `report:approve`（admin）；
  导出使用 `report:export`（editor+）。所有 report/version/export 查询仍先解析 root run
  并写租户/项目作用域。
- `REPORT_ENABLED=false` 时 review/report/export API fail closed 返回 503；研究、RAG 和
  采集 API 不受影响。`REPORT_AUTO_REVIEW=false` 时由 API 手动启动首次 review。
  Review/export 由 `agent` worker 执行，因此需要 `AGENT_ENABLED=true` 并启动该 worker。
- **已知 ACL 边界**：报告继承 root run 的 workspace/project 读取 ACL，不逐条重鉴权
  原始来源；项目成员可能看到 root research run 中已授权证据的摘录。原始来源删除后，
  review 会标记 citation invalid，但既有报告版本和审计记录保留。阶段 8/9 可增加引用级
  ACL 与数据保留策略。

## 持久工作流与事件体系（阶段 7）

- **Transactional outbox**：关键审计/业务变更与 domain event 在同一数据库事务写入
  `domain_events`。事件使用稳定 `event_id`、`event_type`、`event_version`、
  aggregate、tenant、partition key、idempotency key、payload/header。outbox 行带
  RLS、状态机（pending/publishing/published/failed）、lease、attempts、退避与终态
  error。
- **Kafka 发布**：`app.events.worker` 遍历 active tenant，使用
  `FOR UPDATE SKIP LOCKED` 认领 outbox，发布后标记 published。发布失败按配置退避；
  超过上限进入 failed（可查询/人工 replay）。Kafka producer 使用 `acks=all`、
  idempotence、TLS/SASL 配置；生产强制 TLS。
- **事件契约**：事件 envelope 为版本化 JSON；event_version 目前为 1，未知版本由 consumer
  记录为死信。Kafka topic 与 dead-letter topic 均可配置；partition key 保持同一聚合
  在单 partition 内有序。
- **消费幂等**：`processed_events` 以 `(tenant, consumer, event_id)` 唯一约束标记每个
  consumer 已处理事件；handler 副作用与 processed 标记同事务，重复投递不会重复执行。
  handler 失败会重试；达到上限后写入 `event_consumer_failures` 死信记录。
- **Temporal research lifecycle**：`ResearchLifecycleWorkflow` 是确定性 workflow，
  通过 activities 调用现有 research/review/export durable workers。workflow 检查
  run/review 状态、支持 pause/resume（数据库状态）和 cancel、activity 超时/重试，
  直到 run terminal 且 review terminal（或没有 review）；workflow ID 为
  `research-lifecycle-{run_id}`。
- **Temporal starter**：Temporal Worker 内的 starter 从 outbox 的
  `research_run.created` 事件启动 workflow，使用 `processed_events` 标记和
  `REJECT_DUPLICATE` reuse policy，确保重复启动/服务重启不会创建重复工作流；follow-up
  run 由 root workflow 的 batch activities 处理，不单独启动。
- **运行模式**：`TEMPORAL_ENABLED=false` 时 `agent` worker 使用 PostgreSQL 队列；
  true 时启动 Temporal Worker、starter 和 review/export 安全循环。`events` worker
  独立运行 outbox dispatcher；Kafka consumer 在 `EVENT_CONSUMER_ENABLED=true` 时运行。
- **管理 API**：tenant admin 可查询 outbox 状态/类型、重放 failed event、查看 consumer
  dead letter。重放会重新进入 pending 并写审计；consumer 失败记录保留 payload 供后续
  人工处理。
- **生产约束**：生产配置强制 outbox、Kafka、consumer、Temporal、Kafka TLS、
  Temporal TLS；Redis 不作为任务/事件事实来源。Temporal 返回/事件消费都需要
  租户上下文和显式 tenant filter。
- `EVENTS_OUTBOX_ENABLED=false` 时 dispatcher 不运行，但审计仍会写入 outbox
  （配置主要用于维护/禁用派发，不作为删除审计的手段）。Kafka/Temporal 真实服务未在
  本机启动，测试使用 recording publisher、fake Temporal client 和 Temporal test
  environment。

## 多租户与数据库约束

- 身份中间件从已验证 JWT 绑定 `tenant_id` ContextVar；业务代码不能从客户端
  header 获取租户。
- 所有查询显式追加 `tenant_id` 条件；ORM 写入守卫拒绝无上下文或跨租户写入。
- 复合外键把 workspace→organization、project→workspace、membership→scope/user
  约束在同一租户内。
- 每个租户表启用 `ENABLE/FORCE ROW LEVEL SECURITY`，策略比较
  `tenant_id` 与事务级 `app.tenant_id`（`set_config(..., true)`）。无上下文时
  NULL 比较使读写 fail closed。
- **生产 API 必须使用非超级用户数据库角色**；PostgreSQL 超级用户会绕过 RLS。
  本地 Compose 的 `market_intel` 是超级用户，因此本地主要依靠应用层过滤与守卫；
  集成测试通过 `SET ROLE` 验证非超级用户下的 RLS。

## API

所有业务路由位于 `/api/v1`，统一要求 Bearer Token。

| 方法 | 路径 | 权限 |
| --- | --- | --- |
| GET | `/api/v1/me` | 任意已认证用户 |
| GET/POST | `/api/v1/users` | tenant_admin |
| GET/POST | `/api/v1/organizations` | 列表：按成员关系过滤；创建：tenant_admin |
| GET/PATCH | `/api/v1/organizations/{id}` | read / org admin |
| GET/POST | `/api/v1/organizations/{id}/workspaces` | read / org editor+ |
| GET/POST/DELETE | `/api/v1/organizations/{id}/members[/{user_id}]` | org admin |
| GET/PATCH | `/api/v1/workspaces/{id}` | read / editor+ |
| GET/POST | `/api/v1/workspaces/{id}/projects` | read / workspace editor+ |
| GET/POST/DELETE | `/api/v1/workspaces/{id}/members[/{user_id}]` | workspace admin |
| GET/PATCH | `/api/v1/projects/{id}` | read / editor+（ABAC 可能要求 admin） |
| GET/POST/DELETE | `/api/v1/projects/{id}/members[/{user_id}]` | project admin |
| POST | `/api/v1/workspaces/{id}/documents` | workspace editor+；带 `project_id` 时 project editor+ |
| GET | `/api/v1/workspaces/{id}/documents` | 工作区/项目文档 ACL |
| POST | `/api/v1/documents/{id}/versions` | 文档作用域 editor+ |
| POST | `/api/v1/documents/{id}/versions/{version_id}/index` | 文档作用域 editor+ |
| DELETE | `/api/v1/documents/{id}` | 文档作用域 admin |
| GET | `/api/v1/documents/{id}` | 文档 ACL |
| POST | `/api/v1/workspaces/{id}/search` | 工作区/项目 read ACL |
| POST | `/api/v1/workspaces/{id}/web-search` | workspace editor+（外部 Provider，结果不可信） |
| POST/GET | `/api/v1/workspaces/{id}/collection-tasks` | editor+ / workspace content read（`project_id` 仅过滤） |
| GET | `/api/v1/workspaces/{id}/collection-tasks/{task_id}` | workspace content read |
| POST | `/api/v1/workspaces/{id}/collection-tasks/{task_id}/retry` | workspace editor+ |
| GET | `/api/v1/workspaces/{id}/sources` | `workspace:content_read` |
| GET/DELETE | `/api/v1/workspaces/{id}/sources/{source_id}` | `workspace:content_read` / workspace admin |
| POST/GET | `/api/v1/workspaces/{id}/research-runs` | research editor+ / research read（带 `project_id` 用项目作用域） |
| GET | `/api/v1/workspaces/{id}/research-runs/{run_id}` | research read |
| POST | `/api/v1/workspaces/{id}/research-runs/{run_id}/pause` | research control（editor+） |
| POST | `/api/v1/workspaces/{id}/research-runs/{run_id}/resume` | research control（editor+） |
| POST | `/api/v1/workspaces/{id}/research-runs/{run_id}/cancel` | research control（editor+） |
| GET | `/api/v1/workspaces/{id}/research-runs/{run_id}/findings` | research read |
| GET | `/api/v1/workspaces/{id}/research-runs/{run_id}/checkpoints` | research read（回放状态） |
| GET | `/api/v1/workspaces/{id}/research-runs/{run_id}/tool-calls` | research read（只读工具轨迹） |
| POST/GET | `/api/v1/workspaces/{id}/research-runs/{run_id}/review` | research control / report read |
| GET | `/api/v1/workspaces/{id}/research-runs/{run_id}/review/claims` | report read（claim/citation/evidence） |
| GET | `/api/v1/workspaces/{id}/research-runs/{run_id}/report` | report read |
| GET | `/api/v1/workspaces/{id}/research-runs/{run_id}/report/versions[/{version_id}]` | report read |
| POST | `/api/v1/workspaces/{id}/research-runs/{run_id}/report/versions/{version_id}/approve` | report approve（admin） |
| POST | `/api/v1/workspaces/{id}/research-runs/{run_id}/report/versions/{version_id}/reject` | report approve（admin） |
| POST | `/api/v1/workspaces/{id}/research-runs/{run_id}/report/versions/{version_id}/exports` | report export（editor+） |
| GET | `/api/v1/workspaces/{id}/research-runs/{run_id}/report/exports[/{job_id}]` | report read |
| GET | `/api/v1/workspaces/{id}/research-runs/{run_id}/report/exports/{job_id}/download` | report read（仅已完成） |
| GET | `/api/v1/audit-events` | tenant_admin |
| GET | `/api/v1/events` | tenant_admin（outbox 状态/类型过滤） |
| POST | `/api/v1/events/{id}/retry` | tenant_admin（仅 failed event replay） |
| GET | `/api/v1/events/consumer-failures` | tenant_admin（consumer dead letter） |

研究 run 创建示例（随后由 `python -m app.agent.worker` 执行）：

```json
POST /api/v1/workspaces/{workspace_id}/research-runs
{
  "objective": "评估 2026 年企业级市场机会、竞品与客户采用障碍",
  "project_id": null,
  "request_key": "optional-idempotency-key"
}
```

统一错误响应：

```json
{"error": {"code": "forbidden", "message": "The operation is not permitted.",
           "request_id": "...", "retryable": false}}
```

## 配置

所有配置来自环境变量，支持本地 `.env`。完整示例见 [`.env.example`](.env.example)。

| 变量 | 说明 |
| --- | --- |
| `ENVIRONMENT` | `development` / `test` / `staging` / `production` |
| `DATABASE_URL` | 必填，`postgresql+asyncpg://` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 连接池容量 |
| `DB_STATEMENT_TIMEOUT_MS` | 语句/空闲事务超时 |
| `QDRANT_URL` / `QDRANT_API_KEY` | 向量库地址与密钥 |
| `RAG_ENABLED` | 启用文档/搜索路由；false 时 fail closed 503 |
| `OBJECT_STORAGE_BACKEND` | `local` / `s3`；production 必须 `s3` |
| `S3_ENDPOINT_URL` / `S3_BUCKET` / `S3_REGION` | S3/MinIO 配置；production 使用 https |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | 可选（也可使用 IAM role），不得写入代码 |
| `S3_USE_SSL` / `S3_ADDRESSING_STYLE` / `S3_AUTO_CREATE_BUCKET` | 本地 MinIO 常用 path 模式；production 禁止自动建桶 |
| `DOCUMENT_MAX_BYTES` / `DOCUMENT_MAX_PARSED_CHARS` / `DOCUMENT_MAX_PAGES` | 上传/解析上限 |
| `DOCUMENT_MAX_CHUNKS` / `CHUNK_MAX_CHARS` / `CHUNK_OVERLAP_CHARS` | 分块上限与重叠 |
| `DOCUMENT_STATE_LEASE_SECONDS` | processing/deleting 崩溃租约，过期后允许重试 |
| `EMBEDDING_PROVIDER` | `hash`（仅开发）/ `http`；production 必须 `http` |
| `EMBEDDING_API_URL` / `EMBEDDING_API_KEY` | OpenAI 兼容 embedding endpoint；production 必须 https |
| `EMBEDDING_MODEL` / `EMBEDDING_VERSION` / `EMBEDDING_DIMENSION` | 向量模型元数据；维度必须与 collection 一致 |
| `QDRANT_COLLECTION` / `QDRANT_LOCAL_MODE` | collection 名；本地模式仅开发/测试 |
| `SEARCH_CANDIDATE_LIMIT` / `SEARCH_DEFAULT_LIMIT` / `SEARCH_MAX_LIMIT` | 检索候选与返回数量 |
| `COLLECTOR_ENABLED` | 启用外部搜索/采集 API 与 worker |
| `COLLECTOR_USER_AGENT` | 采集器标识；生产应提供可联系信息 |
| `COLLECTOR_ALLOWED_DOMAINS` / `COLLECTOR_BLOCKED_DOMAINS` | 全局域名 allow/block 策略；blocked 优先 |
| `COLLECTOR_ALLOW_PRIVATE_NETWORKS` | 仅 development/test 可用；production 必须 false |
| `COLLECTOR_ALLOWED_PORTS` / `COLLECTOR_ALLOWED_CONTENT_TYPES` | 端口/内容类型白名单；production 仅 80/443 |
| `COLLECTOR_REQUEST_TIMEOUT_SECONDS` / `COLLECTOR_MAX_RESPONSE_BYTES` / `COLLECTOR_MAX_EXTRACTED_CHARS` | 网络/响应/提取上限 |
| `COLLECTOR_MAX_REDIRECTS` / `COLLECTOR_RESPECT_ROBOTS` | 重定向与 robots 策略 |
| `COLLECTOR_ROBOTS_CACHE_SECONDS` / `COLLECTOR_ROBOTS_MAX_BYTES` | robots 缓存与大小 |
| `COLLECTOR_MAX_CONCURRENT_TASKS` / `COLLECTOR_DOMAIN_MIN_INTERVAL_SECONDS` | 全局并发与域名起始速率 |
| `COLLECTOR_TASK_LEASE_SECONDS` / `COLLECTOR_MAX_ATTEMPTS` / `COLLECTOR_RETRY_*` | worker 租约、重试上限/退避 |
| `WEB_SEARCH_PROVIDER` / `WEB_SEARCH_API_URL` / `WEB_SEARCH_API_KEY` | `none`/`http` 搜索 Provider 与凭证 |
| `WEB_SEARCH_API_KEY_HEADER` / `WEB_SEARCH_TIMEOUT_SECONDS` / `WEB_SEARCH_MAX_RESULTS` | 搜索 Provider 细节 |
| `AGENT_ENABLED` | 启用多 Agent 研究 API 与 worker；false 时 fail closed 503 |
| `AGENT_PROVIDER` | `deterministic`（仅开发/测试契约桩）/ `http`；production 必须 `http` |
| `AGENT_MODEL` / `AGENT_API_URL` / `AGENT_API_KEY` / `AGENT_API_KEY_HEADER` | 模型网关标识、https 地址与密钥；密钥不得写入代码 |
| `AGENT_PROVIDER_TIMEOUT_SECONDS` / `AGENT_PROVIDER_MAX_RETRIES` / `AGENT_PROVIDER_RETRY_BASE_SECONDS` | 模型网关超时与有限重试 |
| `AGENT_MAX_RESPONSE_BYTES` / `AGENT_MAX_OBJECTIVE_CHARS` | 模型响应与调研目标上限 |
| `AGENT_MAX_ROUNDS` / `AGENT_MAX_PARALLEL_TASKS` / `AGENT_MAX_TASK_ATTEMPTS` | 轮数、并行 Agent、单任务重试上限 |
| `AGENT_MAX_FINDINGS_PER_TASK` / `AGENT_MAX_FINDINGS` / `AGENT_MAX_EVIDENCE_ITEMS` / `AGENT_MAX_EVIDENCE_CHARS` / `AGENT_MAX_LEADS` | 证据与 finding 数量/文本上限 |
| `AGENT_MAX_INPUT_TOKENS` / `AGENT_MAX_OUTPUT_TOKENS` / `AGENT_MAX_OUTPUT_TOKENS_PER_CALL` | token 预算 |
| `AGENT_MAX_COST_MICROS` / `AGENT_INPUT_COST_MICROS_PER_1K` / `AGENT_OUTPUT_COST_MICROS_PER_1K` | 成本预算与单价；0 表示不设置该成本上限/单价 |
| `AGENT_MAX_TOOL_CALLS` / `AGENT_MAX_TOOL_CALLS_PER_TASK` | 工具调用预算 |
| `AGENT_MAX_SECONDS` / `AGENT_TASK_TIMEOUT_SECONDS` / `AGENT_MIN_FINDINGS_FOR_FOLLOWUP` | 总时长/单任务超时/触发补查的 finding 下限 |
| `AGENT_MAX_CHECKPOINT_BYTES` | 单检查点状态大小上限 |
| `AGENT_MAX_CONCURRENT_RUNS` / `AGENT_POLL_INTERVAL_SECONDS` / `AGENT_RUN_LEASE_SECONDS` / `AGENT_MAX_RUN_ATTEMPTS` | worker 并发、轮询、lease 与恢复上限 |
| `REPORT_ENABLED` / `REPORT_AUTO_REVIEW` | 启用 review/report/export；完成后自动排队审查 |
| `REPORT_MAX_FOLLOWUP_ROUNDS` / `REPORT_MAX_FOLLOWUP_QUESTIONS` | 有限补查轮数/每轮问题数 |
| `REPORT_MAX_CLAIMS` / `REPORT_MAX_EVIDENCE_PER_CLAIM` / `REPORT_MAX_LINEAGE_FINDINGS` | claim/evidence/lineage 处理上限 |
| `REPORT_MAX_EVIDENCE_AGE_DAYS` / `REPORT_MIN_INDEPENDENT_SOURCES` / `REPORT_MIN_SUPPORT_SCORE` | freshness、独立来源与词法支持阈值 |
| `REPORT_CONFLICT_MIN_SHARED_TOKENS` | 冲突启发式的最小共享显著词 |
| `REPORT_MAX_CONTENT_CHARS` / `REPORT_MAX_EXPORT_BYTES` | 报告内容/导出文件上限 |
| `REPORT_REVIEW_*` / `REPORT_MAX_CONCURRENT_REVIEWS` | review lease、重试、轮询与并发 |
| `REPORT_EXPORT_*` / `REPORT_MAX_CONCURRENT_EXPORTS` | export lease、重试、轮询与并发 |
| `EVENTS_OUTBOX_ENABLED` / `EVENT_PUBLISHER` | outbox 总开关 / `log` 或 `kafka`；生产必须 kafka |
| `EVENT_TOPIC` / `EVENT_DEAD_LETTER_TOPIC` / `EVENT_CONSUMER_GROUP` | Kafka topic、死信 topic、consumer group |
| `KAFKA_BOOTSTRAP_SERVERS` / `KAFKA_SECURITY_PROTOCOL` / `KAFKA_SASL_*` | Kafka broker 与 TLS/SASL；生产必须 TLS |
| `EVENT_OUTBOX_*` | dispatcher batch/lease/attempt/退避/轮询 |
| `EVENT_CONSUMER_ENABLED` / `EVENT_CONSUMER_MAX_ATTEMPTS` | consumer 开关、重试/死信上限 |
| `TEMPORAL_ENABLED` / `TEMPORAL_HOST` / `TEMPORAL_TLS` | Temporal 开关、host:port、生产必须 TLS |
| `TEMPORAL_NAMESPACE` / `TEMPORAL_TASK_QUEUE` | workflow 命名空间和 task queue |
| `TEMPORAL_POLL_INTERVAL_SECONDS` / `TEMPORAL_WORKFLOW_MAX_SECONDS` / `TEMPORAL_ACTIVITY_TIMEOUT_SECONDS` | workflow/activity 预算 |
| `OIDC_ENABLED` | production 必须为 true |
| `OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_JWKS_URI` | 签发方、受众、JWKS；production 必须 https |
| `OIDC_ALGORITHMS` | 非对称算法列表，默认 `RS256` |
| `OIDC_TENANT_CLAIM` | 租户 UUID 的签名 claim 名，默认 `tenant_id` |
| `ALLOWED_HOSTS` | TrustedHost 白名单；production 必须显式配置且禁止 `*` |
| `CORS_ALLOWED_ORIGINS` | 逗号分隔或 JSON 数组；默认关闭 |
| `MAX_REQUEST_BODY_BYTES` | 请求体上限，默认 10 MiB |
| `REQUEST_TIMEOUT_SECONDS` | 单请求处理时间上限 |
| `LOG_LEVEL` / `LOG_FORMAT` | 日志级别与 `json`/`console` |
| `DOCS_ENABLED` | 覆盖 OpenAPI 文档开关 |
| `OTEL_ENABLED` / `OTEL_EXPORTER_OTLP_ENDPOINT` | 可选 trace 导出 |

生产环境启动强校验：禁止 `DEBUG`、`DB_ECHO`、非 JSON 日志、通配符 host/CORS；
必须启用 OIDC 且 issuer/JWKS 使用 https；启用 RAG 时必须使用 S3、HTTP embedding、
https endpoint，并禁止本地 Qdrant 模式与自动建桶；启用采集时禁止私网目标、只允许
80/443、要求 S3/TLS，搜索 Provider URL 必须 https；启用 Agent 时 `AGENT_PROVIDER`
必须为 `http`，`AGENT_API_URL` 必须为 https；启用报告时要求 S3/TLS 与 bucket。
阶段 7 在生产还强制 outbox、Kafka、consumer 启用，Kafka 必须 SSL/SASL_SSL，
Temporal 启用且 TLS。敏感字段不出现在 `repr`；日志对
token/authorization/password/api_key 等 key 递归脱敏。

> embedding 模型/版本或维度变更后必须重建索引（重新索引现有版本或写入新版本）；
> Qdrant collection 维度不匹配会阻止启动。

## 审计与可观测性

- 成功变更：`tenant.created`、`user.provisioned`、`organization.created/updated`、
  `workspace.created/updated`、`project.created/updated`、
  `membership.granted/revoked`、`document.uploaded/indexed/deleted`、
  `collection.task_queued/succeeded`、`source.created/updated/deleted`、
  `research_run.created/paused/resumed/canceled/completed/failed/budget_exhausted`、
  `research_review.queued/started/awaiting_followup/completed/failed/followup_created`、
  `report.version_created/version_approved/version_rejected`、
  `report.export_queued/export_succeeded/export_failed`、`event.replayed`，与业务变更
  同事务写入。
- 失败/拒绝：`authorization.denied`、`document.index_failed`、`document.delete_failed`、
  `collection.task_failed`、`source.delete_failed`、`research_review.failed`、
  `report.export_failed`，用独立短事务/终态事务写入，避免请求回滚后丢失。
- 审计只记录 ID、动作、资源类型和受限元数据；不记录 token、请求体、密码、文件名、
  文档正文或网页原文。
- JSON 日志包含 `request_id`、`tenant_id`、`trace_id`/`span_id`；访问日志不含查询串与
  请求体。可选 OpenTelemetry 导出 FastAPI/SQLAlchemy/HTTPX span，健康探针 excluded。
- RAG/采集启用时 readiness 额外检查对象存储与 Qdrant collection（采集仅检查对象存储）。
  Agent 模型网关不纳入 readiness（无标准健康契约）；provider 故障会以 run 失败/重试
  状态和结构化日志体现。

## 测试与检查

```bash
pytest                       # 全部；TEST_DATABASE_URL（环境或 .env）不可用时跳过集成
pytest -m "not integration"  # 单元测试
pytest -m integration        # 真实 PostgreSQL：迁移、API、RLS、CLI
ruff check .
ruff format --check .
mypy app
alembic check                # 模型/迁移漂移检查（需数据库）
```

单元测试覆盖：配置校验、OIDC/JWT、中间件、RBAC/ABAC、文本/PDF 解析、分块与引用
offset、hash/HTTP embedding、Qdrant 内存模式过滤、对象存储路径安全、RRF 融合等。

集成测试覆盖：OIDC 拒绝路径、租户 claim 不匹配、跨租户 404、角色矩阵、JWT
role/permission claim 不被信任、成员授予/撤销、最后组织管理员保护、ABAC
confidential/archived、审计成功/拒绝、租户 suspended、RLS 非超级用户隔离、
复合外键、迁移往返与 `alembic check`；文档上传/索引/检索引用定位、跨租户检索与
删除、workspace/project 文档 ACL、版本切换、幂等重建、索引失败重试、删除失败重试、
删除在对象存储/chunk/Qdrant 的传播；以及搜索 Provider 策略过滤、任务入队幂等、
worker 快照/来源去重、失败重试/人工 retry、私网 URL 拒绝、跨租户任务访问拒绝、
来源删除传播、域名速率限制。

单元测试另覆盖 URL/IP/端口/域名策略、混合 DNS 答案 fail closed、重定向/大小/类型/
压缩限制（本地 HTTP server）、robots 解析/缓存/状态映射、HTML 抽取、搜索 Provider
contract、本地对象存储路径安全和采集状态租约。

阶段 5 单元测试覆盖：LangGraph specialist 并行、结构化 finding、引用/来源校验、
恶意来源提示注入标记、预算耗尽提前停止、轮数上限、专业 Agent 失败后的有限重试、
HTTP 模型网关非法角色/超大响应/瞬时重试/不可信证据 contract、Agent 配置与生产
https 强校验。集成测试覆盖：run 创建/幂等/跨租户 404/项目作用域、只读工具轨迹、
finding 指向真实 WebSource、checkpoint 回放、pending 暂停/恢复、运行中暂停后从
检查点恢复且不重跑已完成工具、运行中撤销成员权限后失败、取消、预算耗尽状态持久化、
项目成员不得读取 workspace 级 run，以及研究表 RLS。

阶段 6 单元测试覆盖：claim/evidence 词法支持、冲突极性、freshness、quote 存在性、
Markdown/DOCX 导出结构。集成测试覆盖：研究完成后自动 review、source/evidence/
claim/citation 派生、引用校验、source 删除后 citation invalid、有限 followup 与
降级报告、报告版本、人工审批权限、审批/导出审计、checksum 完整性、Markdown 导出
任务/下载、重新 review 生成新版本、跨租户报告 404、报告表 RLS。

阶段 7 单元测试覆盖 Temporal workflow 的状态推进（本地 time-skipping test env）、
events/Temporal 配置生产强校验。集成测试覆盖 outbox 发布/重复不重发、publishing
lease 崩溃恢复、失败退避/死信/tenant admin replay、consumer processed_events 幂等、
handler 失败死信、Temporal starter 幂等和 followup 跳过、事件管理 API 权限，以及
event 表 RLS。

CI 在 PostgreSQL 16 service 上执行 lint、格式、类型检查、迁移升级/回滚/再升级、
`alembic check`、全部测试，并构建 runtime Docker 镜像。

## 架构决策记录

- [ADR-0001：模块化单体与分层](docs/adr/0001-modular-monolith.md)
- [ADR-0002：租户隔离底座](docs/adr/0002-tenant-isolation-foundation.md)
- [ADR-0003：配置、密钥与可观测性](docs/adr/0003-configuration-secrets-observability.md)
- [ADR-0004：OIDC 资源服务器边界与租户 claim](docs/adr/0004-oidc-resource-server.md)
- [ADR-0005：RBAC/ABAC 角色与继承](docs/adr/0005-rbac-abac.md)
- [ADR-0006：数据库 RLS 与租户复合外键](docs/adr/0006-row-level-security.md)
- [ADR-0007：对象存储与不可信文档摄取](docs/adr/0007-document-ingestion.md)
- [ADR-0008：混合检索、ACL 回查与版本/删除传播](docs/adr/0008-hybrid-retrieval.md)
- [ADR-0009：搜索 Provider 边界与隔离采集 worker](docs/adr/0009-search-provider-collector.md)
- [ADR-0010：SSRF 防护、快照去重与采集重试](docs/adr/0010-collection-security.md)
- [ADR-0011：LangGraph Supervisor、并行 Agent 与持久检查点](docs/adr/0011-langgraph-supervisor-checkpoints.md)
- [ADR-0012：Agent 工具最小权限、不可信证据与预算治理](docs/adr/0012-agent-tool-security-budgets.md)
- [ADR-0013：独立 claim/evidence/source/citation 与确定性 Reviewer](docs/adr/0013-evidence-review-entities.md)
- [ADR-0014：不可变报告版本、人工审批与导出任务](docs/adr/0014-report-versions-exports.md)
- [ADR-0015：Transactional outbox、Kafka 事件与消费幂等](docs/adr/0015-transactional-outbox-events.md)
- [ADR-0016：Temporal 研究工作流与 DB 队列回退](docs/adr/0016-temporal-research-workflow.md)
- [ADR 模板](docs/adr/0000-template.md)
- [工程规范](docs/engineering-standards.md)
- [Agent 模型网关契约](docs/agent-model-gateway.md)
- [证据审查与报告交付操作说明](docs/report-delivery.md)
- [事件与工作流运行说明](docs/events-and-workflows.md)

## 本阶段限制 / 阶段 8 依赖

- 已实现阶段 7 的 outbox/Kafka 事件与 Temporal research lifecycle；尚未实现企业监测
  与协作（阶段 8）和生产运营治理（阶段 9）。
- Kafka、Redpanda、Temporal 服务未在本机实际启动；事件/工作流逻辑通过 recording
  publisher、Temporal test environment 和 fake client 验证，生产需联调 TLS/SASL、
  consumer group、topic 权限、workflow 命名空间和故障恢复。
- Temporal workflow 只按 root run 生命周期推进；followup run 由 root workflow 的批次
  activity 处理。若 workflow 因 bug/部署终止，需要运维按 workflow ID reset/restart；
  starter 的 processed marker 不会自动重启已失败 workflow。
- `agent` worker 的 Temporal 模式不运行 PostgreSQL research queue；review/export 仍有
  安全循环。运行模式切换时必须排空旧 worker/迁移未完成 run，避免双模式并行。
- 事件 payload 绑定审计动作，当前 `event_version=1`；新增 event schema 必须保持向后
  兼容或显式升级 consumer。consumer dead letter 通过 DB 记录/API 查询，尚未提供自动
  Kafka dead-letter 重放编排。
- `EVENTS_OUTBOX_ENABLED=false` 仅停止 dispatcher，不停止审计写入 outbox；高吞吐部署
  需要按 SLO 调整 batch/lease、Kafka partition 和清理策略。
- Kafka/事件/工作流均未实现每个租户的独立 partition/fairness、配额或跨租户队列隔离；
  tenant_id 通过 RLS、payload、partition key 和显式过滤保证，物理共享 broker/topic。
- Redis 未被用于任务/事件权威状态；尚未实现 Redis 缓存/限流层。
- Reviewer 的证据关系与冲突检测是确定性词法启发式，不包含语义 NLI/领域专家复核；
  它只作为强制 citation 校验之上的降级/标记机制，不能宣称自动事实核查已完成。
- 报告版本/审查/导出仍由 PostgreSQL lease 队列 + review/export 安全循环执行；Temporal
  生命周期 workflow 已覆盖 research/review/export batch，但细粒度 report/export 状态
  机仍在 DB，不是独立 Temporal workflow。跨租户公平性、死信取消和容量治理属于阶段 9。
- run/finding/review/report 的读取 ACL 是 root run 的 workspace/project ACL；报告中的
  证据摘录会提供给有报告读权限的项目成员，不会对每个读者逐条重新鉴权原始来源。
  引用级 ACL 仍需阶段 8/9 配合数据治理补齐。
- 报告导出只支持 Markdown 与 DOCX；PDF、表格/图表、Word 模板、数字签名和外部 DMS
  集成未实现。DOCX 由 python-docx 生成，未在 Microsoft Word/Office 365 全版本做兼容性
  回归。
- 搜索 Provider 只定义通用 HTTP JSON contract，未实现具体商业搜索供应商 SDK；web
  search 返回的未采集 URL 只是 lead，不能作为 finding 证据；必须先由采集流程形成
  WebSource/Snapshot 才可引用。
- 采集只支持 static HTML/plain text/xhtml，不执行 JavaScript、不做浏览器渲染、不采集
  PDF/图片/附件，也不做 OCR。网页抓取在 worker 进程执行，但不是独立网络命名空间/容器；
  生产建议把 collector worker 部署到受控 egress 网络。
- robots 解析覆盖常见 `User-agent`/`Allow`/`Disallow`/`Crawl-delay`，不是完整 RFC
  实现；缓存按 worker 进程，多个 worker 会重复获取。请求只支持 HTTP/1.1，拒绝压缩
  响应，禁止 https→http 降级；DNS 解析使用线程 + 总超时，极端 DNS 卡顿可能留下后台线程。
- URL 规范化只做 scheme/host/默认端口/fragment 处理，不主动移除 tracking 参数；不同
  tracking URL 可能形成不同来源。
- 域名 allow/block 是部署级配置，尚无租户级域名策略、每租户并发/带宽配额；阶段 8/9
  补齐。域名速率是起始速率，不是完整带宽或抓取并发控制。
- 来源是可阅读的外部资料与快照，尚未自动进入 RAG 索引；Agent 通过 `source_search`
  读取已采集快照，不执行新的网页抓取。快照是原始不可信内容，任何下游模型必须按数据
  处理。
- `AGENT_PROVIDER=deterministic` 只是本地契约桩，不具备真实市场研究能力；生产必须
  配置经批准的 HTTP 模型网关。HTTP 网关 contract 仅用 mock transport 验证，未联调
  真实商业/内部模型服务。run 记录创建时的模型名与图版本，worker 配置不一致会
  使 run 失败；模型/图升级时应先排空 pending/paused run 或明确重跑。模型网关会接收
  授权范围内的证据文本，当前未实现 PII 自动脱敏、供应商数据保留策略或跨租户模型
  缓存隔离；这些属于部署治理与阶段 9。
- Agent 工具选择采用有界、固定的只读搜索流程，不向模型开放任意工具调用/代码执行；
  这降低提示注入风险，但也限制复杂自主工具编排，后续可在同一授权边界内扩展。
- 模型接入为单一配置的 HTTP gateway + 服务端预算/超时/有限重试；尚未实现多供应商
  路由、模型级 QPS 限流、跨 run 成本归集或按角色选择模型，阶段 8/9 补齐。
- Reviewer 不调用额外模型；若需要语义关系判断/NLI，应在后续阶段以独立、可审计的
  reviewer provider 扩展，不能复用产生 claim 的同一模型自评。
- followup 通过新建 linked research run 实现，补查轮数/问题数有硬上限；达到上限后
  报告必须保留 gaps/conflicts，不能继续自动循环。
- 持久检查点由应用在 LangGraph superstep 后写入租户表，不是 LangGraph 原生
  checkpointer；暂停/取消/权限变更在 superstep 边界生效，不保证中断进行中的单个
  模型/工具调用；该 superstep 已产生的 finding 会保留供审计。阶段 7 已加入 Temporal
  生命周期编排、outbox 和事件死信；workflow 失败/超时仍需运维 restart/reset。
- failed/budget_exhausted 为终态；本阶段只支持 paused 的 resume，没有 failed 重跑、
  提高预算或所有权转移 API。局部 task 重试在 run 内完成；跨 run 的人工恢复/审批流
  属于阶段 8/9。
- `TEMPORAL_ENABLED=false` 时 agent worker 仍使用 PostgreSQL lease 队列；true 时使用
  Temporal Worker + outbox starter。两种模式不能同时对同一 run 做 research 队列执行，
  切换时必须排空旧模式。极端时钟/网络分区需阶段 9 的故障演练。
- 来源搜索目前是参数化 ILIKE 候选，未建立外部快照全文索引；大规模来源库需要专用
  检索/索引方案。报告生成的 claim 审查/词法冲突为 O(n²) 有界比较（默认 claim 上限
  100），大规模报告需要索引/模型辅助。
- 未实现租户级上传/embedding/采集/Agent/报告配额与成本归集；阶段 8/9 补齐。
- 文档支持 `.txt`/`.md`/`.pdf`；OCR、DOCX、表格/图片与更复杂版式解析未实现；未接入
  恶意文件扫描，解析在受限线程而非独立沙箱进程运行，部署需补充恶意内容检测与资源限制。
- 关键词检索为 PostgreSQL 全文 + 参数化 ILIKE 回退，未接入专用 BM25/OpenSearch；
  大规模数据需要按部署规模评估索引与重排方案。
- 本地 MinIO 镜像在部分网络无法匿名拉取；可使用本地文件存储或企业镜像仓库。
- Qdrant 真实服务在本地未启动（测试使用 qdrant-client 内存模式）；生产需验证真实
  collection、payload index 与容量/延迟。
- 本机 Docker daemon 未运行，MinIO/S3 适配器未做真实服务联调；测试使用本地文件存储。
  S3 适配器使用 boto3 标准接口，部署时需验证桶权限、TLS、加密与生命周期策略。
- 文档索引/删除仍在请求内同步执行并受请求超时限制；采集使用 PostgreSQL 任务队列 +
  独立 worker，但尚未接入 Temporal/Kafka。并发索引/删除与 Qdrant 跨存储一致性采用
  PG 回查、状态租约与可重试状态；阶段 7 已提供 Temporal/outbox，但文档索引/同步删除
  的跨存储事务仍需阶段 9 的容量/故障演练验证。
- 报告导出对象写入与 RAG/采集相同的对象存储；本地 MinIO/S3 未做真实服务联调，生产
  需验证桶权限、TLS、加密、生命周期与导出对象保留策略。
- 本地 Compose 数据库用户是超级用户，RLS 的实际强制需要部署时切换为非超级用户；
  生产角色/权限、密钥轮换、SLO、备份恢复与合规认证属于后续部署阶段，本文不作未验证承诺。
