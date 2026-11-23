# 企业监测、通知、协作与租户配额（阶段 8）

本阶段在既有研究、采集、报告和事件体系之上，增加持续监测、变更告警、项目协作节点
和租户级资源配额，同时保持租户隔离、服务端授权和不可信输入处理约束。

## 1. 能力范围

### 监测订阅

- `monitor_subscriptions` 保存租户/工作区（可选项目）维度的监测订阅：
  `keyword` 或 `competitor` 查询、执行周期、下次运行时间、状态
  （`active` / `paused` / `deleted`）。
- 调度完全使用 PostgreSQL 队列（`monitor_runs`），不依赖外部调度器。
  `python -m app.monitoring.worker` 在每个周期内：
  1. 通过 `FOR UPDATE SKIP LOCKED` 领取到期订阅并创建 `MonitorRun`；
  2. 用 advisory transaction lock 串行化租户配额检查；
  3. 调用已配置的搜索 Provider，对结果做与采集器相同的 URL/SSRF 过滤；
  4. 通过 `enqueue_tasks` 创建采集任务，复用现有采集器的 DNS pinning、
     robots、域名限速和内容快照能力；
  5. 采集任务完成后读取 `web_sources.latest_snapshot_id` 和
     `source_snapshots.content_sha256`，与 `monitor_seen_sources` 比较；
  6. 为新来源或内容哈希变化写入不可变的 `monitor_changes`，再按告警规则创建
     `notification_deliveries`；
  7. 独立领取并发送通知，记录尝试次数、重试时间、响应状态和安全错误码。

运行级 lease、`SKIP LOCKED` 领取和尝试计数保证多个 worker 并行时不会重复处理同一
运行；worker 崩溃后 lease 过期可被重新接管。

### 变更语义

- `new_source`：该订阅第一次见到此来源。
- `content_changed`：同一来源的最新快照 `content_sha256` 与上次记录不同。
- 同一运行内同一来源同一变更类型只记录一次（数据库唯一约束 + 查询去重）。
- `monitor_seen_sources` 只保存快照 ID、内容哈希和时间，不复制网页正文。
- 通知载荷只包含订阅名称、变更类型、严重度、来源标题/URL 和摘要；正文内容不会
  离开系统。所有来源内容按不可信输入处理，载荷显式标记 `"untrusted": true`。

### 通知渠道

- 渠道类型：`webhook` 和 `log`（开发/审计用途）。
- webhook URL 和可选 HMAC signing secret 使用 Fernet 对称加密后存入
  `notification_channels.encrypted_config`。密钥只从
  `NOTIFICATION_ENCRYPTION_KEY` 环境变量/密钥管理服务读取。
- API 响应从不返回 URL、secret 或密文；只有渠道名称、类型、启用状态等元数据。
- 发送前重新执行 URL/DNS 安全校验：仅 HTTPS（开发可显式 `NOTIFICATION_ALLOW_HTTP=true`）、
  仅公网地址、固定已验证 IP 连接、不跟随重定向、限制响应大小和超时。可通过
  `NOTIFICATION_ALLOWED_DOMAINS` 进一步限制目标域后缀。
- 支持 `X-MarketLens-Signature: sha256=<hex>` HMAC（可配置头名），以及
  `X-MarketLens-Delivery` / `X-MarketLens-Idempotency-Key`。接收方应按 delivery ID
  去重。
- 默认最多 5 次指数退避重试；目标被 SSRF 策略拒绝、渠道被禁用、密钥损坏等不可恢复
  错误直接失败并写入审计，不无限重试。

### 租户配额

`tenant_quotas` 为每个租户保存：

| 资源 | 默认上限 | 检查点 |
| --- | --- | --- |
| 活跃研究运行 | 10 | 创建 research run 前 |
| 待处理采集任务 | 100 | 入队采集任务前（按批量数量） |
| 监测订阅 | 20 | 创建订阅前 |
| 并发监测运行 | 2 | 调度新 run 前 |
| 每 24 小时通知投递数 | 1000 | 创建 delivery 前 |

检查使用 `pg_advisory_xact_lock(hashtext('quota:<tenant>:<resource>'))`，在同一事务内
串行化同租户同资源的“计数 + 写入”，避免并发请求同时越过上限。超限返回
`429 quota_exceeded`。租户管理员可通过 `GET/PATCH /api/v1/quotas` 查看和调整限制；
修改本身写入审计和 outbox 事件。

### 项目协作

- `project_comments`：项目级评论，支持一层父评论引用和软删除；作者可编辑/删除
  自己的评论，项目/工作区管理员可管理任意评论。
- `approval_requests`：针对不可变 `report_version` 的协作审批节点，包含指派人、
  请求说明、截止时间和决策记录。同一版本/指派人只能存在一个 pending 节点。
- 审批节点是协作流程记录，**不直接改变报告版本状态**。报告正式审批仍由
  `POST /api/v1/reports/{id}/versions/{version_id}/approve` 的租户/项目管理员执行，
  因为它要求校验报告 checksum。这样避免协作节点绕过报告完整性校验。

## 2. API

所有路由继承 `/api/v1` 前缀，要求 OIDC Bearer Token；授权在服务端完成。

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/workspaces/{workspace_id}/notification-channels` | `notification_channel:manage` |
| POST | `/workspaces/{workspace_id}/notification-channels` | `notification_channel:manage` |
| DELETE | `/workspaces/{workspace_id}/notification-channels/{channel_id}` | `notification_channel:manage` |
| GET | `/workspaces/{workspace_id}/monitors` | `monitor:read` |
| POST | `/workspaces/{workspace_id}/monitors` | `monitor:manage`（项目订阅在项目范围检查） |
| GET | `/workspaces/{workspace_id}/monitors/{subscription_id}` | `monitor:read` |
| PATCH | `/workspaces/{workspace_id}/monitors/{subscription_id}` | `monitor:manage` |
| DELETE | `/workspaces/{workspace_id}/monitors/{subscription_id}` | `monitor:manage` |
| POST | `/workspaces/{workspace_id}/monitors/{subscription_id}/trigger` | `monitor:manage` |
| POST | `/workspaces/{workspace_id}/monitors/{subscription_id}/pause` | `monitor:manage` |
| POST | `/workspaces/{workspace_id}/monitors/{subscription_id}/resume` | `monitor:manage` |
| GET | `/workspaces/{workspace_id}/monitors/{subscription_id}/runs` | `monitor:read` |
| GET | `/workspaces/{workspace_id}/monitors/{subscription_id}/changes` | `monitor:read` |
| GET | `/workspaces/{workspace_id}/monitors/{subscription_id}/deliveries` | `monitor:read` |
| GET/POST | `/projects/{project_id}/comments` | `comment:create` |
| PATCH/DELETE | `/projects/{project_id}/comments/{comment_id}` | 作者本人，或 `comment:manage` |
| GET/POST | `/projects/{project_id}/approval-requests` | `approval:read` / `approval:create` |
| POST | `.../approval-requests/{id}/approve` | 指派对象，或 `approval:decide` + 项目管理员 |
| POST | `.../approval-requests/{id}/reject` | 同上 |
| GET/PATCH | `/quotas` | tenant admin (`quota:read` / `quota:manage`) |

`monitor:read` 不会从父级角色自动继承到项目监测资源；项目级订阅会回到项目上下文
重新检查，避免工作区只读成员通过父级 fallback 读取受限项目。其他权限仍遵循
organization -> workspace -> project 的向下继承规则。

## 3. 配置与运行

```bash
# 生成通知渠道加密密钥（只写入密钥管理服务或本地 .env）
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

必要配置：

- `MONITORING_ENABLED=true`
- `NOTIFICATION_ENCRYPTION_KEY=<Fernet key>`（production 强制）
- `NOTIFICATION_ALLOW_HTTP=false`（production 强制）
- 搜索使用既有 `WEB_SEARCH_PROVIDER` / `WEB_SEARCH_API_URL` 配置；
  未配置搜索 Provider 时监测运行会安全失败并记录错误，不会伪造来源。
- 采集仍由 `python -m app.collector.worker` 执行。

运行监测 worker：

```bash
python -m app.monitoring.worker           # 持续轮询
python -m app.monitoring.worker --once    # 处理一批后退出，适合 CronJob/演练
```

本地 Compose 已增加 `monitor` 服务；它没有入站端口，只访问 PostgreSQL、搜索
Provider 和告警 webhook。

## 4. 运维与安全注意事项

- 监测 worker 与采集 worker 必须能访问相同的对象存储和数据库；否则无法读取快照。
- 所有 worker 都应独立配置数据库连接池，避免与 API 争抢连接。
- 通知重试是 at-least-once；接收方应使用 delivery ID 做幂等。
- 渠道禁用后不再创建新投递；已排队投递会失败并记录 `channel_disabled`。
- 轮换 `NOTIFICATION_ENCRYPTION_KEY` 需要先用旧密钥解密、再以新密钥重新加密所有
  `notification_channels.encrypted_config`；当前版本没有自动多密钥轮换。
- 监测快照中的网页内容仍按不可信输入存储和展示；下游渲染必须转义。

## 5. 已知限制

- 调度器基于 PostgreSQL 轮询，不是 Temporal Schedule；大规模订阅（数十万级）需要
  分片或迁移到专用调度服务。
- 变更检测基于内容哈希和来源 URL，不做 HTML 语义 diff、价格抽取或实体级变更定位。
- 通知渠道仅实现 webhook/log；邮件、企业微信、Slack 等适配器需要新增受控 egress
  实现，不能直接复用任意 URL。
- 通知渠道密钥轮换、密钥版本管理和 KMS 信封加密尚未实现。
- 配额是软上限：已开始的运行/任务不会被中断；并发窗口内仍可能短暂超过上限一个单位。
- 审批节点不驱动报告状态机，也不发送站内信；完整工作流编排留待后续阶段。
- 评论不做富文本、附件、@提及和实时推送；正文最大 5000 字符。
