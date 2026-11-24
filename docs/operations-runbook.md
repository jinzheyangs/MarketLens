# 生产运营手册

本手册面向部署方。所有目标（可用性、延迟、RPO/RTO、成本上限）都必须由部署方
明确配置、测量并签署；仓库中的示例值不构成已达成承诺，也不代表任何合规认证。

## 1. 环境隔离

- 每个环境（dev / staging / production）使用独立的：
  - Kubernetes namespace、Helm release 和 values 文件；
  - Terraform state（远程加密后端 + 锁）与 tfvars；
  - PostgreSQL 实例/数据库、S3 bucket、KMS key、Secrets Manager 条目；
  - Kafka topic 前缀/集群、Temporal namespace、Qdrant collection；
  - OIDC client/audience、搜索/模型供应商凭证。
- `ALLOWED_HOSTS`、CORS、OIDC issuer/audience 必须按环境精确配置，禁止复用生产值。
- 非生产环境不得连接生产数据库或对象存储；CI 使用独立测试库且库名必须包含 `test`。

## 2. 配置与密钥

运行时 Secret 键见 [`deploy/README.md`](../deploy/README.md)。要求：

- 由 External Secrets Operator、CSI Secret Store 或云密钥管理服务注入，不写入镜像、
  Helm values、Git 或日志。
- 数据库、Kafka、Temporal、Qdrant、S3 启用 TLS；生产启动校验会拒绝明文 Kafka 和
  Temporal，以及无 TLS 的数据库配置（由 DSN/驱动约束）。
- `NOTIFICATION_ENCRYPTION_KEY` 是 Fernet 密钥；轮换需要重新加密
  `notification_channels.encrypted_config`，当前没有自动双密钥轮换。
- `METRICS_BEARER_TOKEN` 至少 24 字符；生产启用 metrics 但未配置时启动失败。
- model/search/provider 密钥按最小权限和供应商配额配置。

## 3. 发布与回滚

### 发布

1. 合并到 main 后由 CI 执行 lint、类型检查、迁移往返、单元/集成测试、Helm/Terraform
   校验、依赖与镜像扫描、SBOM。
2. 发布 workflow 构建多架构镜像、生成 SBOM、keyless 签名并推送到 GHCR。
3. 在目标环境执行：
   ```bash
   helm upgrade --install marketlens deploy/helm/marketlens \
     -n marketlens -f values.production.yaml --atomic --timeout 20m
   ```
   pre-upgrade Hook 会先运行 `alembic upgrade head`；失败会阻止发布（`--atomic`）。
4. 验证 `/health/ready`、`/metrics`、关键 API 与 worker 日志。

### 回滚

```bash
helm rollback marketlens <revision> -n marketlens --wait --timeout 10m
```

**Helm 回滚不会回滚数据库迁移。** 迁移必须遵循 expand/contract（先加列/表，后切换
读写，最后删除旧结构），确保上一个应用版本可在当前 schema 上运行。若必须回滚破坏性
迁移，先停止写入，按已批准的数据库恢复流程执行；不要直接 `alembic downgrade` 生产。

### 数据库迁移失败

- 查看 Hook Job：`kubectl -n marketlens logs job/<release>-migrate-<revision>`。
- 不要重复执行未知状态的迁移；先检查 `alembic current` 和数据库锁。
- 迁移期间停止发布；如已部分提交（PostgreSQL DDL 事务通常原子，但长时间索引创建
  例外），按备份/前向修复方案处理。

## 4. SLO 与告警

建议起始 SLO（需部署方批准并测量）：

| SLI | 目标 | 告警 |
| --- | --- | --- |
| API 可用性（非 5xx 比例） | 99.9% / 30 天 | 5m 错误率 > 1.44%（14.4x burn）持续 10m |
| API 延迟 | p95 < 500 ms，p99 < 1 s | p95 > 1s 持续 10m |
| Worker 可用性 | 期望副本数全部可用 | Deployment unavailable > 0 持续 15m |
| 迁移 Job | 成功 | 失败 > 0 持续 5m |

规则与仪表盘在 `deploy/observability/`。指标不含 tenant/user 标签；如需按租户计费，
请通过已授权的管理 API（`/api/v1/usage/costs`）而不是 Prometheus 标签。

## 5. 故障处理

租户管理员可通过 `GET /api/v1/operations/queues` 获取当前租户的研究、采集、审查、
导出、监测、通知、审批、outbox 和 consumer failure 积压数量。该接口需要管理员
权限，不能替代 Prometheus；worker 指标缺口见 `deploy/observability/README.md`。

### API 5xx / 可用性下降

1. 检查 `marketlens_http_5xx:ratio5m` 与最慢路由。
2. `kubectl get pods,deploy`，查看 readiness 和重启原因。
3. 检查 PostgreSQL 连接池/延迟、Qdrant、Kafka、Temporal、对象存储。
4. 若与发布相关，`helm rollback`；否则扩容 API 或降级非关键功能。
5. 保留 request ID、时间窗和受影响租户范围；不要在日志中粘贴令牌或正文。

### 工作线程积压

- `collector`/`agent`/`events`/`monitor` 均使用数据库 lease 队列，可以安全水平扩容。
- 若 lease 频繁过期，检查进程是否被 OOM/驱逐，以及 `*_LEASE_SECONDS` 是否短于任务
  实际耗时。
- 重复副作用由 `processed_events`、delivery key、状态机幂等保护；人工重放通过
  `/api/v1/events`（租户管理员）执行并审计。

### 依赖不可用

- Kafka 不可用：outbox 保留事件，恢复后重试；不要清空 outbox。
- Temporal 不可用：Research lifecycle 启动暂停；DB worker 模式可用于受控降级，但
  需要显式配置变更和验证。
- Qdrant 不可用：RAG 检索失败应显式降级，不得伪造引用。
- S3 不可用：采集/导出失败并重试；不要丢失对象键和快照元数据。

## 6. 备份与恢复

脚本：`deploy/scripts/backup-postgres.sh`、`verify-backup.sh`、
`restore-postgres.sh`。

- 备份使用 `pg_dump --format=custom`，生成 SHA-256；可选上传 S3 并启用 KMS。
- 建议：每日全量 + 云厂商 PITR；备份保留与 RPO 匹配。Terraform 示例配置 14 天自动
  备份，但 PITR 窗口和跨区域副本需按业务要求额外配置。
- 每季度至少一次恢复到隔离环境，记录：
  - 实际恢复耗时（RTO）；
  - 可恢复的最新时间点（RPO）；
  - 数据校验（行数、报告 checksum、样本对象读取）。
- 恢复前必须确认目标库名（脚本强制 `RESTORE_CONFIRM`），默认不做 destructive clean。
- 对象存储需要单独启用版本控制和生命周期；数据库恢复与对象存储一致性由演练验证。

**RPO/RTO 是部署方配置目标，不是本仓库已验证的保证。**

## 7. 数据保留

`python -m app.ops.retention [--execute] [--tenant <uuid>]`：

- 默认 dry-run，只统计不删除。
- `--execute` 需要 `RETENTION_DESTRUCTIVE_ENABLED=true`。
- 只处理：
  - 超过 `RETENTION_OPERATIONAL_DAYS` 的 `processed_events`；
  - 超过该期限且已解决的 consumer failures；
  - 超过 `RETENTION_EVENT_DAYS` 且已发布的 outbox events。
- **不会**删除报告、证据、claim、文档、评论、监测变更等业务记录。
- 删除幂等键前必须确认 Kafka/consumer 保留窗口小于该期限，否则旧事件可能被重新
  处理。保留窗口应大于消息系统最大重放/延迟时间。
- 业务数据生命周期（客户删除请求、法务保留、归档）需要独立审批流程和租户级工具；
  当前未提供自动归档到冷存储。

### 通知渠道密钥轮换

```bash
# 1. 部署配置：新 primary + 旧 previous
# NOTIFICATION_ENCRYPTION_KEY=<new>
# NOTIFICATION_PREVIOUS_ENCRYPTION_KEY=<old>
python -m app.ops.notification_keys            # dry-run，确认 failed=0
python -m app.ops.notification_keys --execute  # 重新加密并写审计
python -m app.ops.notification_keys            # 再次验证
```

确认全部渠道 rotated、failed=0 后才移除 `NOTIFICATION_PREVIOUS_ENCRYPTION_KEY`。
如果出现 failed，保留旧密钥并检查渠道密文/密钥来源；轮换不会删除 or 覆盖无法解密
的配置。该流程只支持一把 previous key，不支持 KMS 信封加密。

## 8. 成本与配额

- `GET /api/v1/usage/costs` 按 workspace/project/model 聚合 token、tool calls 和
  `cost_micros`，仅租户管理员可读。
- `GET/PATCH /api/v1/quotas` 管理研究和监测配额；超限返回 429。
- 模型价格必须由部署方配置 `AGENT_INPUT_COST_MICROS_PER_1K` 和
  `AGENT_OUTPUT_COST_MICROS_PER_1K`；为 0 时成本预算不生效。
- 成本数据来自应用记账，不等同于云账单；应定期与供应商账单对账。

## 9. 安全扫描与供应链

- `.github/workflows/security.yml`：pip-audit、Trivy 镜像扫描、Syft SPDX SBOM、
  SARIF 上传。
- `.github/workflows/release.yml`：多架构构建、provenance、SBOM、cosign keyless
  签名与 SBOM attestation。
- 部署前验证镜像 digest/签名；禁止使用 `latest`。
- 依赖升级由 Dependabot 提议，仍需通过完整测试和人工审查。
- 本仓库未执行真实云环境渗透测试、合规审计或第三方认证。

## 10. 容量、负载与故障演练

- k6 场景：`deploy/load/k6-smoke.js`；在批准的测试租户和窗口运行。
- 负载目标应基于压测结果设置 HPA 与数据库连接池；不要仅凭 CPU 阈值推断容量。
- 故障演练建议至少覆盖：
  1. 删除/驱逐 API Pod（验证 PDB、readiness、滚动恢复）；
  2. 数据库主备切换（验证重连、RPO/RTO）；
  3. Kafka/Temporal/Qdrant 短暂不可用（验证 outbox、重试、降级）；
  4. worker OOM/lease 过期（验证重新领取与幂等）；
  5. 恢复备份到隔离环境并运行 smoke。
- 每次演练记录时间线、检测方式、实际 RTO、数据丢失窗口、后续修复项。

## 11. 日常检查清单

- [ ] 无长期 unavailable worker/API 副本
- [ ] 5xx、p95、数据库连接池、磁盘/存储空间在阈值内
- [ ] outbox 无持续增长的 pending/dead letter；consumer failure 已处理
- [ ] 备份成功且校验通过；恢复演练未过期
- [ ] 证书、OIDC JWKS、Kafka/Temporal TLS 证书未临近过期
- [ ] 依赖/镜像扫描无未处置的 critical/high
- [ ] 配额、成本与供应商账单无异常
- [ ] 审计日志可查询，敏感字段未进入日志/追踪
