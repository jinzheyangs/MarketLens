# 企业级多角色市场调研 Agent — 后端

面向企业团队的市场情报与调研平台后端。最终架构与交付阶段见
[`docs/项目蓝图.md`](docs/项目蓝图.md)。

> **当前实现范围：阶段 3「资料摄取与企业 RAG」。**
> 已完成：阶段 1 工程底座、阶段 2 身份/租户/RBAC/ABAC/审计，以及阶段 3 的对象存储、
> 文档上传/解析/分块/版本、embedding 与 Qdrant、混合检索/重排、候选阶段 ACL、
> PostgreSQL 回查、索引幂等/重建、版本切换、删除传播和失败恢复。
> 尚未实现：外部搜索/采集（阶段 4）、多 Agent（阶段 5）、证据报告（阶段 6）、
> Temporal/Kafka（阶段 7）等。

## 技术栈

- Python 3.12、FastAPI、Pydantic v2 / pydantic-settings
- SQLAlchemy 2（asyncpg）、Alembic、PostgreSQL 16、RLS、全文检索
- PyJWT（非对称签名校验）、Email Validator
- S3 兼容对象存储（本地 MinIO / 生产 S3）、boto3
- Qdrant（qdrant-client）、可替换 embedding provider、pypdf
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
```

或使用 Makefile（`make install-dev`、`make migrate`、`make run`）。

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
| GET | `/api/v1/audit-events` | tenant_admin |

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
https endpoint，并禁止本地 Qdrant 模式与自动建桶。敏感字段不出现在 `repr`；日志对
token/authorization/password/api_key 等 key 递归脱敏。

> embedding 模型/版本或维度变更后必须重建索引（重新索引现有版本或写入新版本）；
> Qdrant collection 维度不匹配会阻止启动。

## 审计与可观测性

- 成功变更：`tenant.created`、`user.provisioned`、`organization.created/updated`、
  `workspace.created/updated`、`project.created/updated`、
  `membership.granted/revoked`、`document.uploaded/indexed/deleted`，与业务变更
  同事务写入。
- 失败/拒绝：`authorization.denied`、`document.index_failed`、`document.delete_failed`，
  用独立短事务写入，避免请求回滚后丢失。
- 审计只记录 ID、动作、资源类型和受限元数据；不记录 token、请求体、密码、文件名或
  文档内容。
- JSON 日志包含 `request_id`、`tenant_id`、`trace_id`/`span_id`；访问日志不含查询串与
  请求体。可选 OpenTelemetry 导出 FastAPI/SQLAlchemy/HTTPX span，健康探针 excluded。
- RAG 启用时 readiness 额外检查对象存储与 Qdrant collection。

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
复合外键、迁移往返与 `alembic check`；以及文档上传/索引/检索引用定位、跨租户检索与
删除、workspace/project 文档 ACL、版本切换、幂等重建、索引失败重试、删除失败重试、
删除在对象存储/chunk/Qdrant 的传播。

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
- [ADR 模板](docs/adr/0000-template.md)
- [工程规范](docs/engineering-standards.md)

## 本阶段限制 / 阶段 4 依赖

- 未实现外部搜索 Provider、网页抓取、SSRF/DNS 重绑定防护、robots/速率限制、快照与
  来源去重；这些属于阶段 4，外部网页仍按不可信输入处理。
- 未实现 LangGraph 多 Agent、claim/evidence/source 数据模型、证据审查、报告导出、
  Temporal/Kafka、监测与通知。
- 文档支持 `.txt`/`.md`/`.pdf`；OCR、DOCX、表格/图片与更复杂版式解析未实现；未接入
  恶意文件扫描，解析在受限线程而非独立沙箱进程运行，部署需补充恶意内容检测与资源限制。
- 关键词检索为 PostgreSQL 全文 + 参数化 ILIKE 回退，未接入专用 BM25/OpenSearch；
  大规模数据需要按部署规模评估索引与重排方案。
- 本地 MinIO 镜像在部分网络无法匿名拉取；可使用本地文件存储或企业镜像仓库。
- Qdrant 真实服务在本地未启动（测试使用 qdrant-client 内存模式）；生产需验证真实
  collection、payload index 与容量/延迟。
- 本机 Docker daemon 未运行，MinIO/S3 适配器未做真实服务联调；测试使用本地文件存储。
  S3 适配器使用 boto3 标准接口，部署时需验证桶权限、TLS、加密与生命周期策略。
- 未实现后台任务队列；当前索引/删除在请求内同步执行并受请求超时限制，后续阶段应迁移
  到持久工作流/worker。并发索引/删除与 Qdrant 跨存储一致性采用 PG 回查、状态租约与
  可重试状态，仍需阶段 7 的 outbox/工作流完善。
- 未实现租户级上传/embedding 配额与速率限制；阶段 8/9 补齐成本治理。
- 本地 Compose 数据库用户是超级用户，RLS 的实际强制需要部署时切换为非超级用户；
  生产角色/权限、密钥轮换、SLO、备份恢复与合规认证属于后续部署阶段，本文不作未验证承诺。
