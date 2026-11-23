# ADR-0011：LangGraph Supervisor、并行 Agent 与持久检查点

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：5

## 背景

阶段 5 需要 Supervisor 制定调研计划，把行业、竞品、用户研究并行分派给专业 Agent，
输出结构化 finding，并在 token/成本/时长/轮数/工具调用预算内继续、暂停、恢复或终止。
调研运行是长任务，进程重启后不能丢失已完成步骤，也不能让 Agent 无限互聊。所有 run、
finding、trace 和检查点必须租户隔离并受服务端授权。

## 决策

1. **LangGraph StateGraph**：使用 Supervisor + 三个专业 Agent；`Send` 实现并行
   fan-out，聚合后由 Supervisor 判断有限补查或 finalize。轮数、并行度、单任务重试和
   finding 数都有硬上限，不存在自由对话循环。
2. **有界工具流程**：专业 Agent 使用固定的只读证据收集流程（RAG、已采集来源、搜索
   lead），模型只进行证据分析，不能任意选择工具或执行代码。工具注册与权限见
   ADR-0012。
3. **结构化状态**：状态为 JSON 可序列化的 TypedDict，包含 objective/scope、plan、
   task results、findings、errors、usage、phase 和 stop_reason；并行分支使用
   reducer 合并列表。状态中不包含请求 token、数据库 DSN 或模型密钥。
4. **应用级持久检查点**：runner 使用 LangGraph `astream(stream_mode="values")`，
   每个 superstep 后将完整状态写入 `research_checkpoints`，并在同一事务更新 run 的
   usage/round/plan/checkpoint 计数。数据库行带 `tenant_id`，启用 FORCE RLS 和复合
   外键，API 读取前重新授权。
5. **不采用 LangGraph 原生 PostgresSaver**：其表结构不含本项目 tenant/workspace
   scope 与 RLS 策略，直接使用会形成绕过租户模型的旁路。当前实现以应用级检查点满足
   “可恢复、可回放”，并在阶段 7 由 Temporal 补齐跨服务持久编排。
6. **DB lease worker**：独立 `python -m app.agent.worker` 遍历 active tenant，
   `FOR UPDATE SKIP LOCKED` 认领 run，使用 lease + heartbeat；worker 崩溃后过期 lease
   可从最后一个 checkpoint 重新执行。API 进程不执行长调研。
7. **暂停/取消**：API 对 pending run 直接改状态，对 running run 设置 request flag；
   runner 在 superstep 边界保存 checkpoint 后转 paused/canceled。恢复时从最新检查点
   重建状态，跳过已成功的 task，并清除 pause 控制标记后重新进入图。
8. **局部失败恢复**：单个专业 Agent 的模型/工具失败只生成该 task 的失败结果；其他
   task 的 finding 保留。失败 task 在 `max_task_attempts` 和剩余预算内重试；成功 task
   不重复执行。最终可标记 completed/partial/failed/budget_exhausted。
9. **运行中重新授权**：每个 superstep 后重新加载请求者的 active user、成员关系、
   run 控制权限、RAG document scope 和 workspace/tool capability。权限/能力变化或
   控制权丢失会把 run 标记为 failed（authorization_changed），停止后续工具/模型调用；当前
   superstep 已产生的证据保留供审计。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| LangGraph + 应用级租户检查点 | 图编排清晰；满足 RLS/回放 | 自维护检查点格式 | 采纳 |
| LangGraph 原生 PostgresSaver | 现成 | 表无 tenant/RLS，旁路隔离 | 拒绝 |
| 单节点 asyncio.gather | 简单 | 无显式图、状态和可恢复边界 | 拒绝 |
| API 进程 BackgroundTasks | 少一个进程 | 部署/重启/扩缩容不可靠 | 拒绝 |
| 立即引入 Temporal | 长流程成熟 | 属于阶段 7，提前引入复杂 | 延后 |

## 后果

- 正面：并行研究、显式状态、检查点/回放、暂停恢复和预算终止可测试；run 状态在
  进程崩溃后可恢复；所有持久数据受 RLS。
- 负面：检查点按 superstep 写 PostgreSQL，存在写放大；暂停/取消不能中断进行中的单个
  模型调用；自定义检查点不是 LangGraph 原生格式，升级时需维护兼容性。
- 安全与隔离：工具和证据 scope 由服务端按当前 DB 角色重建；checkpoint/finding/trace
  均 tenant-scoped。模型网关可能收到授权证据文本，需要部署方数据治理。
- 后续：阶段 6 在同一 run/finding 之上增加 Reviewer 和报告；阶段 7 用 Temporal 管理
  跨服务生命周期、outbox、死信和取消，LangGraph 仍负责图内协作。

## 验证

单元测试覆盖并行 specialist、预算耗尽、轮数上限、失败 task 有限重试、引用拒绝和
提示注入标记。集成测试覆盖 run API 授权/跨租户 404、项目成员不得读取 workspace 级 run、worker
完成、finding 指向真实来源、checkpoint 回放、运行中暂停后从检查点恢复且不重复工具
调用、运行中撤销权限后失败、取消、预算状态和 RLS 表策略。迁移执行 upgrade/downgrade/upgrade 与 `alembic check`。
