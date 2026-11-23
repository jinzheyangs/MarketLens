# 事件与工作流运行说明（阶段 7）

## 运行模式

| 模式 | 配置 | 进程 |
| --- | --- | --- |
| 本地 DB queue | `TEMPORAL_ENABLED=false` | `python -m app.agent.worker` |
| Temporal | `TEMPORAL_ENABLED=true` + host/TLS | `python -m app.agent.worker`（自动切 Temporal Worker） |
| 事件派发（log） | `EVENT_PUBLISHER=log` | `python -m app.events.worker` |
| Kafka | `EVENT_PUBLISHER=kafka` + brokers/TLS | `python -m app.events.worker` |
| Kafka consumer | `EVENT_CONSUMER_ENABLED=true` | 同上，consumer 与 dispatcher 并发 |

Docker Compose 提供 `redpanda`、`temporal`、`events` 服务；本地 `.env.example`
默认 `EVENT_PUBLISHER=log`、`TEMPORAL_ENABLED=false`，因此不要求真实 Kafka/Temporal
即可启动数据库队列模式。

## Outbox

业务/审计事件在业务事务内写入 `domain_events`。dispatcher 按 active tenant：

1. `FOR UPDATE SKIP LOCKED` 认领 pending 或 lease 过期的 publishing 事件；
2. 设置 lease/worker/attempts，提交事务；
3. 在事务外发布 Kafka/log；
4. 成功标记 published；失败退避或进入 failed。

failed event 可由 tenant admin 通过
`GET /api/v1/events?status=failed` 查询，并用
`POST /api/v1/events/{id}/retry` 重放。重放会重置 attempts 并写 `event.replayed`
审计。

## Consumer

Kafka consumer：

- 自动 offset 重置 earliest，手动 commit；
- `processed_events` 每个 `(tenant, consumer, event_id)` 唯一；
- handler 与 processed marker 同事务；
- handler 内有限重试，达到 `EVENT_CONSUMER_MAX_ATTEMPTS` 后写
  `event_consumer_failures`（dead_letter），不会无限阻塞；
- 未知 event_version 立即 dead-letter；
- tenant context 从 envelope 绑定，DB 侧 RLS 仍生效。

事件 payload 直接来自审计的受限 details；不要把网页原文、token、密码放入事件 details。

## Temporal

`ResearchLifecycleWorkflow`：

```text
get_run_state
  ├─ paused -> sleep
  ├─ terminal -> 等待 review terminal（若无 review 直接完成）
  └─ pending/running -> research batch -> review batch -> export batch -> sleep
```

workflow ID 为 `research-lifecycle-{run_id}`，重复启动被拒绝。starter 读取
`research_run.created` outbox 事件，`processed_events(consumer=temporal-starter)` 防
重复启动；followup run 不单独启动 workflow。

Temporal activity 通过现有 ResearchRunner/ReviewRunner/ExportRunner 执行，数据库仍是
权威状态；workflow 只负责生命周期与重试。暂停/恢复使用现有 API 改 DB 状态，cancel
后 workflow 观测到 canceled 并退出。

## 故障与运维

- outbox publishing lease 过期会被其他 dispatcher 重新认领；消费者至少一次。
- Kafka 重复消息由 processed_events 幂等吸收。
- workflow 失败/超时需用 Temporal CLI 按 workflow ID 检查/restart/reset；starter 不会
  自动重启失败 workflow。
- 切换 Temporal 模式前排空 PostgreSQL agent queue；review/export 安全循环仍运行。
- 生产要求 Kafka TLS、Temporal TLS、consumer enabled；真实 broker/history 的容量、
  partition、retention、lag 和 ACL 由部署方验证。
