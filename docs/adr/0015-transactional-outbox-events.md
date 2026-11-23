# ADR-0015：Transactional outbox、Kafka 事件与消费幂等

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：7

## 背景

调研、采集、审查、报告和后续监测会产生需要可靠传递的领域事件。直接在业务事务内调用
Kafka 会产生双写不一致；先写业务再发包也可能丢事件。事件消费者必须能处理重复投递，
不能让一次失败无限阻塞 topic，且需要死信/人工恢复和租户隔离。

## 决策

1. **同事务 outbox**：`domain_events` 与业务/审计变更在同一 PostgreSQL 事务写入；
   事件包含 event_id/type/version、tenant、aggregate、actor、payload、headers、
   partition key、idempotency key、occurred_at。outbox 表启用 RLS。
2. **至少一次派发**：独立 `app.events.worker` 遍历 active tenant，使用
   `FOR UPDATE SKIP LOCKED` 认领；先以 lease/attempt/max_attempt 标记 publishing，
   事务提交后再发 Kafka，成功标记 published，失败退避/终态 failed。发布在 DB 事务外，
   避免长事务夹住 broker。
3. **Kafka producer**：`aiokafka`，`acks=all` + idempotence；生产强制 SSL/SASL_SSL，
   topic/partition key 可配置。开发使用 log publisher，不依赖真实 broker。
4. **事件 schema/version**：envelope JSON + `event_version=1`；consumer 遇到未知版本写
   dead-letter，不做静默解释。新增字段必须兼容，破坏性变更升级 version。
5. **消费幂等**：`processed_events` 以 `(tenant, consumer, event_id)` 唯一，handler 与
   marker 同事务；重复投递返回成功但不重复副作用。consumer name 是幂等作用域，升级
   handler 时使用新 consumer group/name。
6. **失败/死信**：consumer 内有限重试；超过上限写 `event_consumer_failures`，保留
   payload/header/error，供 tenant admin 查询和后续审批/人工处理。outbox failed
   event 提供 tenant admin replay，重放会重置 attempts 并写审计。
7. **管理/观测**：tenant admin 可查询 outbox 状态/type、死信；不暴露 Kafka 凭证；
   event payload 与审计 details 同受脱敏约束。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| DB outbox + Kafka | 事务一致、可重放 | 额外表/worker | 采纳 |
| 业务事务内直接发 Kafka | 简单 | 双写不一致/阻塞 | 拒绝 |
| 仅审计表轮询 | 无 Kafka | 扩展/事件生态弱 | 拒绝 |
| 无 processed_events | 简单 | 重复副作用 | 拒绝 |
| 无死信 | 不阻塞 | 数据黑洞 | 拒绝 |

## 后果

- 正面：业务变更与事件原子；至少一次派发；重复消费幂等；失败可人工恢复；事件有版本。
- 负面：outbox 需要清理/归档；Kafka 至少一次仍可能重复，必须依赖 consumer 幂等；
  tenant 共享 topic，超大租户需要分区/fairness 运营。
- 安全：事件带 tenant，outbox/processed/dead-letter RLS；Kafka TLS/SASL 生产强制；
  不把 Redis 当权威。
- 后续：阶段 8 在 consumer 上增加监测/通知 handler；阶段 9 增加 topic/retention/容量、
  lag 监控和分区治理。

## 验证

单元/集成测试覆盖 outbox 发布、重复不重发、publishing lease 崩溃恢复、失败退避/
dead-letter、tenant admin replay、consumer 重复事件幂等、handler 失败死信、事件 RLS。
真实 Kafka/Redpanda 未在本地启动。
