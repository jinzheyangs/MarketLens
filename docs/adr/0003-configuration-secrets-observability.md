# ADR-0003：环境配置、密钥边界与可观测性

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：1、9

## 背景

平台包含数据库、向量库、模型供应商和采集服务等外部依赖。密钥若进入代码、镜像、
镜像构建参数或日志，会形成长期泄露风险。阶段 1 需要建立统一配置入口和安全默认值，
并预留 trace/metrics 接入点，但不虚构生产 SLO。

## 决策

1. **唯一配置入口**：`app/core/config.py` 的 `Settings`（pydantic-settings）从环境变量
   读取；本地可用 `.env`，该文件被 `.gitignore` 忽略。
2. **无密钥默认值**：`DATABASE_URL` 必填；`QDRANT_API_KEY` 等敏感字段为 `SecretStr`，
   设置 `repr=False`，错误信息使用 `hide_input_in_errors=True`。
3. **服务 URL 约束**：Qdrant/OTLP URL 必须是 http(s)、有 host、不得内嵌用户名密码；
   凭证使用独立 secret 字段。
4. **生产强校验**：禁止 `DEBUG`、`DB_ECHO`、非 JSON 日志、通配符 host/CORS；OTel 开启
   必须有导出 endpoint。日志不记录请求体、查询串、header、DSN。
5. **日志**：stdout 结构化 JSON，包含 request_id/tenant_id/trace_id/span_id；敏感 key
   递归脱敏；Uvicorn 访问日志由自有中间件替代。
6. **Trace**：OpenTelemetry 可选启用；FastAPI/SQLAlchemy/HTTPX instrumentation，
   OTLP/HTTP 导出；不采集请求体，不导出健康探针 span。
7. **健康探针**：`/health/live` 不访问依赖；`/health/ready` 检查 DB 与已配置的 Qdrant，
   失败只返回组件状态，详情仅进服务端日志。

## 明确不在本阶段

- 密钥管理服务（Vault/KMS/云 Secret Manager）的具体接入实现与轮换流程。
- Prometheus/Grafana/Loki/Sentry 部署、告警规则、SLO/RPO/RTO 数值。
- 日志留存、审计合规、数据脱敏策略的最终验证。

这些在阶段 9 由部署方按真实环境配置和演练；本文不声称已满足任何合规认证。

## 后果

- 正面：开发与生产配置一致、密钥不出现在代码/镜像/日志；可观测性可按环境开关。
- 负面：本地必须显式创建 `.env`；生产漏配关键变量会启动失败（预期行为）。
- 风险：环境变量仍可能被进程转储或日志外泄；生产需配合最小权限、密钥轮换和日志访问控制。
- 部署方需自行提供 OTLP collector 与日志平台。

## 验证

- 单测覆盖必填 DSN、scheme 校验、敏感 repr、URL 凭证拒绝、生产强校验、日志脱敏。
- 健康探针测试覆盖 200/503 与错误细节不泄露。
- CI 使用独立测试数据库，迁移可升级/回滚/再升级。
