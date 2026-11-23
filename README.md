# 企业级多角色市场调研 Agent — 后端

面向企业团队的市场情报与调研平台后端。最终架构与交付阶段见
[`docs/项目蓝图.md`](docs/项目蓝图.md)。

> **当前实现范围：阶段 1「架构与工程底座」。**
> 已建立模块化单体骨架、配置与密钥边界、PostgreSQL/Alembic、统一错误与日志、
> 请求防护、健康探针、Docker Compose、CI、测试和 ADR。组织/身份/RBAC/审计
> （阶段 2）、RAG（阶段 3）、搜索采集（阶段 4）、多 Agent（阶段 5）尚未实现，
> 本仓库不会假装它们已可用。

## 技术栈

- Python 3.12、FastAPI、Pydantic v2 / pydantic-settings
- SQLAlchemy 2（asyncpg）、Alembic、PostgreSQL 16
- Qdrant（本阶段仅作为 Compose 服务与就绪探针依赖，尚未建立 collection）
- OpenTelemetry（可选，OTLP/HTTP）
- pytest / pytest-asyncio、Ruff、mypy（strict）、GitHub Actions
- Docker / Docker Compose

## 目录结构

```text
app/
  main.py            # uvicorn 入口
  factory.py         # create_app：中间件、生命周期、路由装配
  api/               # 路由、依赖、统一异常处理
  core/              # 配置、上下文、错误、日志、中间件、trace
  db/                # Base、tenant mixin/守卫、引擎、模型
  schemas/           # API schema
  services/          # 应用服务（当前为健康检查）
alembic/             # 迁移环境与版本
tests/unit/          # 无需 PostgreSQL 的测试
tests/integration/   # 需要 TEST_DATABASE_URL 的 PostgreSQL 测试
docs/adr/            # 架构决策记录
docs/engineering-standards.md
```

## 快速开始

### 1. 配置

```bash
cp .env.example .env
# 至少修改 POSTGRES_PASSWORD，并保证 DATABASE_URL 中的密码一致
```

生产/预发必须由密钥管理服务或编排平台注入环境变量；`.env` 仅用于本地开发且已被
`.gitignore` 忽略。代码中没有任何密钥默认值。

### 2. 启动依赖

```bash
docker compose up -d postgres qdrant
```

Compose 只把端口绑定到 `127.0.0.1`，避免把数据库/向量库暴露到局域网。Qdrant
在本地开发中未启用 API key；生产配置请使用 `QDRANT_API_KEY`。

Postgres 首次初始化时会额外创建 `market_intel_test`（见
`docker/postgres/init/`），供集成测试使用。若卷已存在，需手工创建或执行
`docker compose down -v` 后重建（会删除本地数据）。

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

- `GET /health/live`：进程存活，不访问外部依赖。
- `GET /health/ready`：检查 PostgreSQL；配置了 `QDRANT_URL` 时同时检查 Qdrant。
  依赖不可用返回 **503**，响应不包含 DSN、异常文本或上游响应体。
- 开发环境（非 production）默认开放 `/docs`、`/redoc`、`/openapi.json`；
  production 默认关闭，可用 `DOCS_ENABLED=true` 显式覆盖（不推荐）。

### Docker 全量运行

```bash
cp .env.example .env   # 修改 POSTGRES_PASSWORD
docker compose up -d --build
curl http://127.0.0.1:8000/health/ready
```

Compose 中的一次性 `migrate` 服务会在 API 启动前执行 `alembic upgrade head`。
需要手动迁移时可运行 `docker compose run --rm api alembic upgrade head`。
镜像以非 root 用户运行；不在构建阶段写入任何密钥。

## 配置

所有配置来自环境变量，支持本地 `.env`。主要变量见
[`.env.example`](.env.example) 与 `app/core/config.py`。

| 变量 | 说明 |
| --- | --- |
| `ENVIRONMENT` | `development` / `test` / `staging` / `production` |
| `DATABASE_URL` | 必填，必须使用 `postgresql+asyncpg://` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 连接池容量 |
| `DB_STATEMENT_TIMEOUT_MS` | 数据库语句与空闲事务超时 |
| `QDRANT_URL` / `QDRANT_API_KEY` | 向量库地址与密钥；密钥不写代码 |
| `ALLOWED_HOSTS` | TrustedHost 白名单；非 production 未设置时仅允许 localhost，production 必须显式配置且禁止 `*` |
| `CORS_ALLOWED_ORIGINS` | 逗号分隔或 JSON 数组；默认关闭 |
| `MAX_REQUEST_BODY_BYTES` | 请求体上限，默认 10 MiB |
| `REQUEST_TIMEOUT_SECONDS` | 单请求处理时间上限 |
| `LOG_LEVEL` / `LOG_FORMAT` | 日志级别与 `json`/`console` 格式 |
| `DOCS_ENABLED` | 覆盖 OpenAPI 文档开关；production 默认关闭 |
| `OTEL_ENABLED` / `OTEL_EXPORTER_OTLP_ENDPOINT` | 可选 trace 导出 |

生产环境启动前进行强校验：禁止 `DEBUG`、`DB_ECHO`、非 JSON 日志、`ALLOWED_HOSTS=*`、
CORS 通配符，以及缺少 endpoint 却开启 OTel。敏感字段在 `Settings.__repr__`
中不出现，日志对敏感 key 递归脱敏。

## 安全与多租户基线

本阶段建立以下约束，完整授权在阶段 2 落地：

- **租户字段**：`TenantScopedMixin` 提供非空、带索引的 `tenant_id`；所有业务表必须继承。
- **写入守卫**：ORM `before_flush` 强制租户范围内写入——无租户上下文拒绝；新行自动
  绑定当前租户；跨租户创建/修改/删除拒绝（`app/db/tenant_guard.py`）。
- **查询辅助**：`tenant_scoped_select()` 在没有租户上下文时 fail closed。
- **租户上下文来源**：仅允许服务端可信代码绑定（`app/core/context.py`）。
  **绝不接受客户端 `X-Tenant-ID` 等 header 直接作为授权依据**；阶段 2 会在身份认证
  与资源授权成功后绑定。
- **外部输入不可信**：请求体大小限制、超时、严格 Pydantic 校验、trusted host、
  默认关闭 CORS；日志不记录请求体、查询串或 header。网页/上传内容的提示注入防护
  属于后续采集与 Agent 阶段，本阶段不宣称已具备。
- **错误响应**：统一 `{"error": {...}}`，包含稳定错误码与 `request_id`；不泄露堆栈、
  内部异常文本或数据库信息。
- **响应头**：`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、
  `Cache-Control: no-store` 等；production 追加 HSTS。
- **关联性**：`X-Request-ID` 经过安全字符集/长度校验后才复用，否则重新生成；
  日志包含 `request_id`、`tenant_id`、`trace_id`/`span_id`（启用 trace 时）。

## 数据库与迁移

```bash
alembic upgrade head
alembic downgrade -1
alembic revision --autogenerate -m "describe change"
```

- 当前迁移创建 `tenants` 根表（租户注册表）以及约束命名规范。组织/工作区/项目、
  成员、角色在阶段 2 加入。
- 迁移 URL 从应用配置读取，不写入 `alembic.ini`。
- 所有涉租户表必须带 `tenant_id`；索引/外键/约束使用统一命名约定。
- 时间字段使用 `timestamptz`；应用生成的时间戳为 UTC。部署时建议将数据库与容器
  时区配置为 UTC（本阶段不强制会话 `TimeZone`，避免依赖特定镜像的时区数据）。
- 集成测试会拒绝在库名不包含 `test` 的数据库上运行破坏性初始化。

## 测试与检查

```bash
pytest                     # 全部测试；TEST_DATABASE_URL（环境或 .env）不可用时跳过集成
pytest -m "not integration"  # 仅单元测试
pytest -m integration      # 仅 PostgreSQL 集成测试
ruff check .
ruff format --check .
mypy app
alembic check              # 模型与迁移漂移检查（需数据库）
```

集成测试覆盖：迁移后表结构/约束、租户注册表 CRUD、无上下文写入拒绝、写入自动绑定、
跨租户写入拒绝、查询辅助只返回当前租户数据。`TEST_DATABASE_URL` 未导出时会尝试读取
本地 `.env`；集成测试会拒绝在库名不含 `test` 的数据库上执行破坏性初始化。

CI（`.github/workflows/ci.yml`）在 PostgreSQL 16 service 上执行 lint、格式、类型检查、
迁移升级/回滚/再升级、`alembic check` 和全部测试，并构建 runtime Docker 镜像。

## 可观测性

- 默认 JSON 日志到 stdout；Uvicorn 访问日志关闭，由中间件输出结构化访问日志
  （方法、路由、状态、耗时、request_id；不含查询串与 body）。
- 可选 OpenTelemetry：FastAPI、SQLAlchemy、HTTPX instrumentation，OTLP/HTTP 导出；
  健康探针不产生 span。示例环境变量见 `.env.example`。
- 本阶段不提供 Prometheus/Grafana/Loki 部署、SLO 数值或备份恢复承诺；这些属于阶段 9，
  需由部署方验证并配置。

## 架构决策记录

- [ADR-0001：模块化单体与分层](docs/adr/0001-modular-monolith.md)
- [ADR-0002：租户隔离底座](docs/adr/0002-tenant-isolation-foundation.md)
- [ADR-0003：配置、密钥与可观测性](docs/adr/0003-configuration-secrets-observability.md)
- [ADR 模板](docs/adr/0000-template.md)
- [工程规范](docs/engineering-standards.md)

## 本阶段已知限制 / 阶段 2 依赖

- 尚无身份认证、OIDC 边界、RBAC/ABAC、审计事件；`tenant_context` 目前只由测试/未来
  身份层显式绑定。
- 尚未启用 PostgreSQL RLS；查询层强制过滤将随授权服务一起加入。
- 无业务 API、对象存储、文档解析、Qdrant collection、搜索/采集、LangGraph、Temporal、
  Kafka。
- `tenants` 表只是未来的租户根注册表，不代表组织/工作区/项目模型已完成。
- 本阶段未做负载测试、灾备、SLO、合规认证或生产容量承诺。
