# 未完成事项与生产验收清单

本文是项目当前唯一的“未完成事项”总表，更新于 2026-11-24。

状态说明：

- **已完成**：代码、迁移、测试和本地验证均已完成，不需要继续开发。
- **待生产验证**：代码已实现，但只在本地 Docker/CLI 或测试环境验证，生产资源配置、
  多节点、TLS、凭证和故障演练尚未完成。
- **未实现**：当前代码中没有该能力，需要后续开发。
- **外部阻塞**：需要云账号、真实供应商、IdP、证书、审计或产品决策。

> 仓库内的 Docker 联调使用：PostgreSQL 16、Qdrant 1.12.5、Adobe S3Mock、
> Redpanda 24.2.7、Temporal CLI dev server。S3Mock 不是 MinIO；Temporal CLI 不是
> 生产集群。真实联调结果和复现方式见 [`real-integration.md`](real-integration.md)。

---

## 1. 当前已完成并通过本地验证

| 能力 | 状态 | 备注 |
| --- | --- | --- |
| 多租户 RBAC/ABAC、RLS、复合外键 | 已完成 | 集成测试覆盖跨租户拒绝 |
| OIDC resource server 校验 | 已完成 | 测试使用本地 RSA/JWKS；真实 IdP 未接入 |
| 文档上传、解析、分块、对象存储 | 已完成 | 真实 S3Mock 上传/读取已验证 |
| Qdrant 混合检索、租户/项目过滤 | 已完成 | 真实 Qdrant 1.12.5 已验证 |
| 安全采集、SSRF/DNS pinning、robots | 已完成 | 外部真实网页和搜索供应商未联调 |
| LangGraph Supervisor/专业 Agent/预算 | 已完成 | deterministic provider 已测；真实模型网关未联调 |
| 证据审查、claim/evidence/citation、报告版本 | 已完成 | Reviewer 为确定性词法规则 |
| Markdown/DOCX 导出 | 已完成 | PDF/表格/签名未实现 |
| Transactional outbox、Kafka、consumer 幂等/死信 | 已完成 | 真实 Redpanda 发布/消费/outbox 已验证 |
| Temporal research lifecycle | 已完成 | 真实 Temporal dev server 已验证 |
| 监测订阅、变更检测、告警 | 已完成 | 搜索 Provider/真实网页未联调 |
| Webhook/Slack/email/log 通知 | 已完成 | 真实 Slack/SMTP 端点未联调 |
| 通知渠道更新、previous-key 轮换 | 已完成 | KMS/多密钥版本未实现 |
| 租户配额、并发限制 | 已完成 | 软上限，不中断已启动任务 |
| 项目评论、审批节点、取消/过滤 | 已完成 | 审批节点不驱动报告状态机 |
| 租户采集域名策略 | 已完成 | 无每租户带宽/并发连接限制 |
| Prometheus API 指标、SLO 规则、Grafana 看板 | 已完成 | 未在真实 Prometheus/Grafana 栈执行 |
| 全局队列指标 exporter | 已完成 | 无单 worker lease/QPS 指标 |
| 成本归集 API | 已完成 | 只覆盖 research run 应用记账 |
| 运维数据保留 | 已完成 | 只清理运维事件表，不处理业务数据 |
| 备份/校验/恢复脚本 | 已完成 | 本地真实 PG 演练 43 表；云 RDS/PITR 未演练 |
| Helm/Terraform 样例 | 已完成静态校验 | 未在真实 K8s/AWS apply |
| SBOM/扫描/签名 CI 定义 | 已完成工作流文件 | 未在本仓库实际跑通全部工具 |

---

## 2. P0：生产上线前必须完成

### 2.1 Kubernetes / Helm 真实部署

**状态：待生产验证 / 外部阻塞**

- `helm lint`、`helm template` 已通过，但未在真实 EKS/GKE/AKS 集群安装。
- 需要验证：
  - Ingress、TLS、DNS、ServiceAccount/IRSA；
  - PodDisruptionBudget、HPA、topology spread；
  - NetworkPolicy 与 Prometheus 抓取端口；
  - 迁移 Job 与 ExternalSecret 的启动顺序；
  - 镜像签名验证和 admission policy。

**解决办法：**

```bash
terraform apply           # staging 账号
helm upgrade --install marketscope deploy/helm/marketscope \
  -n marketscope -f values.staging.yaml --atomic --timeout 20m
kubectl -n marketscope get pods,jobs,svc,networkpolicy
```

### 2.2 Terraform 覆盖范围

**状态：未实现（样例范围限制）**

当前 Terraform 只创建 RDS PostgreSQL、S3、KMS、Secrets Manager。未创建：

- VPC、子网、NAT、路由；
- EKS 集群、节点组、IRSA；
- MSK/Redpanda、Temporal、Qdrant；
- ACM/Route53/Ingress；
- 日志/监控后端和告警路由。

**解决办法：** 在 `deploy/terraform` 增加独立模块或在平台仓库中组合现有模块；
不要把状态文件、密码或私钥提交到 Git。

### 2.3 生产数据库高可用与恢复

**状态：待生产验证**

- 已在本机 PostgreSQL 完成 logical backup → checksum → restore 演练。
- 未验证：
  - RDS Multi-AZ failover；
  - PITR 恢复到指定时间点；
  - 对象存储与数据库恢复的一致性；
  - 跨区域灾备。

**解决办法：**

1. 在 staging 配置 RDS Multi-AZ + 自动备份 + PITR。
2. 记录实际 RPO/RTO，不要使用示例值。
3. 每季度在隔离环境恢复并执行 smoke：
   - `GET /health/ready`
   - RAG 检索
   - 报告 checksum 校验
   - worker 队列消费

### 2.4 真实 OIDC IdP

**状态：外部阻塞**

代码使用 OIDC resource-server 校验，但真实企业 IdP 未接入。

**解决办法：**

- 配置 `OIDC_ISSUER`、`OIDC_AUDIENCE`、`OIDC_JWKS_URI`。
- 验证 JWKS 轮换、时钟偏移、错误 token、停用用户。
- 可使用 Keycloak 作为 staging IdP；生产接入企业 IdP。
- 不要把 JWT 中的角色/权限当作授权结论，角色仍从数据库 membership 加载。

### 2.5 真实模型网关与搜索供应商

**状态：外部阻塞**

- `AGENT_PROVIDER=deterministic` 只是本地契约桩。
- `WEB_SEARCH_PROVIDER=http` 未对真实商业搜索 API 联调。

**解决办法：**

- 配置受控 HTTPS 模型网关和搜索 Provider；
- 验证超时、重试、预算、成本记账、内容安全；
- 对模型网关做 PII/DLP 评估，见 3.3；
- 禁止把供应商密钥写入代码、镜像或日志。

### 2.6 Kafka/Temporal/Qdrant 生产形态

**状态：待生产验证**

- 本地 Redpanda 单节点、Temporal CLI dev server、Qdrant 单节点已验证基础路径。
- 未验证：
  - Kafka TLS/SASL、多 broker、topic 权限、consumer group 重平衡；
  - Temporal 持久化后端、namespace、worker 滚动升级、workflow reset；
  - Qdrant 集群、副本、payload index 在大数据量下的性能；
  - Qdrant client 1.20 与 server 1.12.5 的版本兼容警告。

**解决办法：**

- 固定 server/client 兼容版本；
- 在 staging 做 broker/节点故障和恢复演练；
- Temporal 失败 workflow 的 reset/restart 流程纳入 runbook；
- 配置 Kafka/Temporal TLS 证书轮换。

### 2.7 生产密钥与密钥轮换

**状态：部分完成**

- 支持通知渠道 Fernet 加密和一把 previous key 轮换。
- 未实现 KMS 信封加密、密钥版本表、自动轮换、HSM。
- `NOTIFICATION_ENCRYPTION_KEY` 仍由环境/密钥管理服务注入。

**解决办法：**

- 使用云 KMS GenerateDataKey/Decrypt；
- 数据库保存 encrypted data key + key version；
- 轮换只重加密 data key；
- 增加密钥禁用/审计和回滚路径。

### 2.8 安全扫描与供应链门禁

**状态：待执行**

已添加：

- `pip-audit`
- Trivy 镜像扫描
- Syft SPDX SBOM
- cosign keyless 签名
- Dependabot

但本仓库没有实际跑通全部工具，也没有 admission controller 拦截。

**解决办法：**

- 在 CI 中至少跑通一轮；
- 将 critical/high 设为阻断；
- 验证镜像 digest/签名后再部署；
- SBOM 归档并与发布记录关联。

---

## 3. P1：生产前应完成的功能缺口

### 3.1 PDF / 表格导出

**状态：未实现**

当前只支持 Markdown 和 DOCX。

**解决办法：**

- 使用 WeasyPrint 或 headless Chromium 渲染 HTML → PDF；
- 内置合法授权中文字体；
- 增加 `ExportFormat.PDF`、导出 worker 分支、MIME、大小限制；
- 表格/图表需要固定模板和可复现数据快照；
- 建立 PDF 文本抽取和引用定位测试。

### 3.2 HTML 语义 diff / 价格与产品变化

**状态：未实现**

监测只比较 URL 和内容哈希，无法区分“价格变了”还是“导航栏变了”。

**解决办法：**

- 快照解析 DOM/结构化字段；
- 页面模板识别；
- 文本 diff + 结构化字段 diff；
- 增加 `price_changed`、`product_changed` 等变更类型；
- 用回归语料评估抽取准确率；
- 严格清洗 HTML，禁止脚本/事件属性。

### 3.3 PII/DLP 脱敏

**状态：未实现**

发送给模型网关的证据文本没有自动 PII 脱敏。

**解决办法：**

- 在模型网关前增加 DLP 层；
- 邮箱、手机号、身份证、银行卡、API key 等规则；
- 使用可逆 tokenization，保证模型引用能映射回原文；
- 每租户策略开关；
- 加密保存映射表并审计；
- 不要直接替换文本导致 citation 校验失效。

### 3.4 每租户抓取带宽/并发控制

**状态：部分实现**

已有：

- 租户域名 allow/block；
- 采集任务数量配额。

未实现：

- 每租户并发连接数；
- 每租户抓取带宽；
- 每租户域名速率；
- 按租户的公平调度。

**解决办法：**

- 新增 `tenant_collection_limits`；
- collector 使用每租户令牌桶/并发槽位；
- 超限延迟入队或返回 429；
- 与现有 advisory-lock 配额体系统一。

### 3.5 多模型供应商路由/QPS

**状态：未实现**

当前是单一配置的 HTTP gateway。

**解决办法：**

- provider registry、价格表、健康检查、熔断；
- 每租户/每角色预算和 QPS；
- 记录 provider/model/价格版本；
- 禁止静默跨供应商切换；
- 对模型输出做供应商响应校验。

### 3.6 Worker 单实例指标

**状态：部分实现**

已有：全局队列深度 exporter。

未实现：

- 单 worker lease；
- 单 worker QPS；
- 最后心跳；
- 处理耗时分布；
- worker 内部重试/失败指标。

**解决办法：**

- 每个 worker 启动 Prometheus HTTP server；
- 暴露 `worker_claims_total`、`worker_errors_total`、
  `worker_last_success_timestamp_seconds`、`worker_active_leases`；
- Helm 为每个 worker 增加 metrics Service/ServiceMonitor；
- 或增加 `worker_heartbeats` 表由统一 exporter 读取。

### 3.7 审批节点驱动报告状态机

**状态：产品决策未定**

当前审批节点是协作记录，不改变报告版本状态。

**解决办法：**

- 先明确审批是“会签/知会”还是“正式发布门禁”；
- 若为门禁，批准时必须重新校验：
  - 报告 checksum；
  - 证据完整性；
  - 审批人权限；
- 增加 `draft → in_review → approved → published` 状态机；
- 禁止项目编辑通过协作审批绕过正式报告审批。

### 3.8 业务数据保留、法务保留、客户删除

**状态：未实现**

运维数据保留任务只清理 `processed_events`、已解决的 consumer failures、
已发布的 outbox events；不删除业务记录。

**解决办法：**

- 制定每类业务数据保留期；
- 新增法务 hold 表；
- 先归档到加密冷存储，再删除热库；
- 客户删除请求覆盖数据库、S3、Qdrant、outbox 和备份策略；
- 删除操作异步、可审计、可重试，并生成完成证明；
- 备份中的删除按备份保留策略处理，需法务确认。

### 3.9 通知渠道残余缺口

**状态：部分完成**

已实现：webhook、Slack、email、log。

未实现：

- 企业微信/钉钉 adapter；
- 邮件退信（bounce）、投诉、抑制列表；
- HTML 邮件模板；
- 每租户发件域名/SMTP relay；
- SPF/DKIM/DMARC 配置与验证；
- 附件和大正文策略。

**解决办法：**

- 新增 adapter，不允许任意 URL 绕过 egress 策略；
- 接入 SES/企业邮件网关事件回执；
- 建立 suppression list；
- 配置并验证 SPF/DKIM/DMARC；
- 对附件做病毒扫描和大小限制。

### 3.10 通知 exactly-once

**状态：不可实现（传输语义限制）**

HTTP/Slack/SMTP 无法保证端到端 exactly-once。

**实际方案：**

- 保持 at-least-once；
- delivery ID 作为幂等键；
- 接收方必须去重；
- 内部 consumer 使用 `processed_events`；
- 在 SLA 中明确“至少一次 + 幂等消费”。

---

## 4. P2：阶段 1–7 遗留技术限制

这些限制不影响当前核心闭环，但在大规模或高要求场景需要继续演进。

| 项目 | 当前状态 | 解决办法 |
| --- | --- | --- |
| Temporal workflow 失败恢复 | 需要人工 reset/restart | 增加 workflow 管理 API/运维脚本；把 reset 步骤纳入 runbook |
| Agent failed/budget_exhausted 重跑 | 仅支持 paused resume | 增加受控 rerun、预算调整、所有权转移 API |
| 报告 ACL | 继承 root run 的 workspace/project ACL | 增加引用级/来源级重新鉴权 |
| Reviewer | 确定性词法启发式 | 引入独立 NLI/专家复核 provider，不能复用生成模型自评 |
| 报告并发审查 | O(n²) 有界比较 | 大数据量报告增加索引/分批/模型辅助 |
| DOCX 兼容性 | python-docx 生成 | 在 Word/Office 365 版本矩阵做回归 |
| 采集渲染 | 只支持静态 HTML/纯文本/XHTML | 增加受控浏览器渲染、PDF/图片/OCR |
| robots | 常见规则实现 | 完整 RFC 解析、跨 worker 协调缓存 |
| 域名速率 | 进程内起始速率 | 全局/租户级分布式限速 |
| RAG 来源 ACL | 报告级 ACL | 引用级重鉴权、来源删除传播治理 |
| 文档索引/删除 | 请求内同步、跨存储一致性 | 专用索引任务、Temporal/outbox 编排、容量演练 |
| Redis | 未使用 | 仅在需要时引入缓存/限流，不能保存权威任务状态 |
| 多租户 broker 公平性 | 共享 topic、partition key/RLS | 租户 partition/fairness、配额和隔离策略 |
| 主要云托管服务 | 本地/样例 | 按实际云厂商接入 MSK/Temporal Cloud/Qdrant 托管服务 |
| 多区域/容灾 | 未实现 | 跨区域备份、只读副本、灾难恢复演练 |

---

## 5. 外部阻塞项及需要谁提供

| 项目 | 需要什么 | 负责方 |
| --- | --- | --- |
| 真实云部署 | AWS/GCP/Azure 账号、VPC、EKS、IAM | 平台/云团队 |
| 真实 OIDC | IdP tenant、client、JWKS、测试用户 | 身份团队 |
| 模型网关 | HTTPS endpoint、API key、配额、数据协议 | AI/采购团队 |
| 搜索供应商 | API key、配额、合规协议 | 数据/采购团队 |
| SMTP/Slack | 企业邮件网关、发件域名、Slack 工作区 | IT/协作团队 |
| Kafka/Temporal/Qdrant 托管 | 集群、TLS/SASL、namespace、备份策略 | 平台团队 |
| RPO/RTO | 业务目标、恢复演练窗口 | 业务/运维负责人 |
| 合规/渗透测试 | 第三方评估、法务/安全审批 | 安全/合规团队 |
| 业务数据保留 | 法务保留策略、客户删除 SLA | 法务/产品 |

---

## 6. 建议上线顺序

1. **P0 基础设施**：staging K8s/Terraform、真实 OIDC、PostgreSQL/Qdrant/Kafka/Temporal 托管服务。
2. **P0 安全**：TLS/SASL、KMS/密钥管理、镜像签名、网络策略、审计验证。
3. **P0 恢复**：RDS PITR、Qdrant/对象存储恢复、故障演练、实际 RPO/RTO 记录。
4. **P1 产品**：真实模型/搜索、PDF、PII、退信处理、审批状态机、业务数据保留。
5. **P1 可观测性**：单 worker 指标、真实 Prometheus/Grafana、告警路由和 on-call。
6. **P2 性能**：k6 压测、容量规划、租户公平性、大规模索引/报告优化。
7. **合规**：渗透测试、数据保护评估、供应链审计。

---

## 7. 生产验收 Definition of Done

上线前至少满足：

- [ ] staging 完整 `terraform apply` + `helm upgrade --atomic` 成功
- [ ] 真实 OIDC 登录/退出/JWKS 轮换通过
- [ ] 真实 PostgreSQL/Qdrant/Kafka/Temporal 连通并通过 smoke
- [ ] Kafka/Temporal/S3/Qdrant TLS 与凭证轮换验证
- [ ] 备份、PITR、恢复演练完成并记录 RPO/RTO
- [ ] RLS 在非超级用户生产角色下验证
- [ ] 通知 webhook/Slack/email 至少各有一个真实端点验证
- [ ] 镜像扫描无未处置 critical/high
- [ ] 镜像签名、SBOM、provenance 可查询
- [ ] Prometheus/Grafana 告警路由和 on-call 演练
- [ ] k6/容量测试达到批准目标
- [ ] 业务数据保留/删除/法务 hold 流程批准
- [ ] 安全评审/渗透测试完成，剩余风险有签字记录
