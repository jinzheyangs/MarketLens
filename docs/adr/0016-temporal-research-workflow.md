# ADR-0016：Temporal 研究工作流与 DB 队列回退

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：7

## 背景

research、review、followup、report/export 是跨多个 worker/步骤的长流程。阶段 5/6
使用 PostgreSQL lease 队列已经可以恢复，但缺少跨服务生命周期、确定性重放、活动
超时/重试和运维级 workflow 状态。阶段 7 需要 Temporal 管长流程，同时保留本地/测试
无 Temporal 的可运行路径。

## 决策

1. **Temporal workflow**：`ResearchLifecycleWorkflow` 是确定性 workflow，输入
   tenant_id/run_id/budget，循环读取 run/review 状态，按 batch activities 驱动现有
   `ResearchRunner`、`ReviewRunner`、`ExportRunner`。workflow 不直接访问 DB/网络。
2. **Activities**：`get_run_state`、`run_research_batch`、`run_review_batch`、
   `run_export_batch` 在 Temporal activity 中执行，带 tenant context、start-to-close
   timeout 和有限 RetryPolicy。activity 本身幂等（DB lease/fencing/finding dedupe）。
3. **Workflow ID**：`research-lifecycle-{run_id}`，Temporal reuse policy
   `REJECT_DUPLICATE`。重复 starter/重启不会创建重复 lifecycle。
4. **Outbox starter**：Temporal Worker 内 starter 从 outbox 的
   `research_run.created` 事件启动 workflow；`processed_events`
   (`temporal-starter`) 记录已启动，start 失败不写 marker，已存在 workflow 视为成功。
   followup run 不单独启动，由 root workflow 的 batch activity 处理。
5. **暂停/恢复/取消**：API 继续写 PostgreSQL 状态/request flag；workflow 每次轮询
   read-only activity 的 run/review 状态，paused 时 sleep，canceled 时结束。Temporal
   状态不替代业务数据库事实。
6. **运行模式**：`TEMPORAL_ENABLED=false` 使用 PostgreSQL agent worker；true 时
   agent worker 启动 Temporal Worker + starter，并运行 review/export 安全循环，避免
   手工/延迟 review 在 workflow 结束后无人处理。生产配置强制 Temporal TLS。
7. **失败恢复**：activity 超时/失败由 Temporal RetryPolicy 处理；workflow 终止/超时需
   运维按 workflow ID restart/reset。starter 不会自动重启已失败 workflow，避免隐式
   重复执行；`processed_events` 与 workflow ID 保证启动幂等。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| Temporal workflow + DB activities | 长流程可靠、可视化 | 运维 Temporal | 采纳 |
| 纯 PostgreSQL worker | 简单 | 跨服务编排弱 | 保留 fallback |
| workflow 内直接访问 DB | 少 activity | 非确定性/重试困难 | 拒绝 |
| API 进程同步执行 | 简单 | 超时/重启丢失 | 拒绝 |
| 自动无限重启失败 workflow | 表面恢复 | 重复副作用/成本 | 拒绝 |

## 后果

- 正面：长生命周期可见/可重试；workflow ID/processed marker 幂等；无 Temporal 时仍可
  本地运行；DB 仍是业务事实源。
- 负面：Temporal 服务和 namespace/queue 运维；batch activity 可能处理同 tenant 的
  其他 run；workflow 失败需人工 restart；双模式切换必须排空旧 worker。
- 安全：activity 继承 tenant context，RLS 与现有授权；Temporal 生产 TLS；事件/outbox
  RLS；不把 Temporal history 作为业务数据唯一副本。
- 后续：阶段 8 可将定时监测放到 Temporal Schedule；阶段 9 增加 worker 版本、build ID、
  任务队列容量和 workflow 运维手册。

## 验证

Temporal time-skipping test environment 验证 workflow 状态推进、activity 重试和终态；
starter 集成测试验证 outbox 启动幂等/followup 跳过；DB worker 全量回归测试保留。
真实 Temporal 服务未在本机启动。
