# ADR-0009：搜索 Provider 边界与隔离采集 worker

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：4

## 背景

外部情报需要可替换的搜索供应商和网页采集能力。供应商凭证不能写入代码，外部结果不能
直接成为受信任来源；采集是高风险网络操作，不能放在 API 请求进程里无限执行，也不能靠
超级用户或 RLS 绕过读取跨租户任务。

## 决策

1. **搜索 Provider 接口**：定义 `SearchProvider.search(query, limit)`；内置通用 HTTP
   JSON 适配器，endpoint/凭证/header 由环境配置。结果仅作为候选 URL/title/snippet，
   统一标记 `untrusted=true`，不自动入库。
2. **采集 worker 独立进程**：`python -m app.collector.worker` 无入站端口，遍历 active
   tenant，显式绑定 tenant context 后查询/更新任务；使用 `FOR UPDATE SKIP LOCKED`
   认领任务，不依赖超级用户绕过 RLS。API 只负责授权、入队、读取和 retry。
3. **DB 任务队列**：`collection_tasks` 保存 URL/domain/status/attempts/next attempt/
   lease/active_key；同一 tenant+workspace+canonical URL 的 active 任务去重；终态任务
   释放 active_key。worker 崩溃后过期 lease 可重新认领，超过最大尝试次数进入 failed。
4. **状态机与重试**：`pending → running → succeeded/retry/failed`；可重试网络错误
   指数退避，策略/解析/格式错误终止；API 可对 failed/canceled 人工 retry。每任务保存
   安全 error_code/message，不保存原文。
5. **快照与来源**：原始响应写对象存储，元数据和提取文本写 PostgreSQL；来源按
   workspace+canonical URL hash 去重，快照按 content hash 去重。所有表带 tenant_id 与
   RLS。
6. **来源访问**：来源是 workspace-wide 资料，读取要求真实组织/工作区角色
   (`workspace:content_read`)，不接受项目成员的祖先只读回退；项目任务按 project 角色
   管理；来源删除要求 workspace admin。
7. **审计**：collection.task_queued/succeeded/failed/retried、
   source.created/updated/deleted/delete_failed；不记录网页正文、token 或完整 URL 查询。
8. **关闭策略**：`COLLECTOR_ENABLED=false` 时 API 返回 503；worker 不启动。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| API 内同步抓取 | 简单 | 阻塞请求、无法重试/限速 | 拒绝 |
| 独立 worker + DB 队列 | 隔离、可恢复、无额外服务 | 仍需阶段 7 持久编排 | 阶段 4 采纳 |
| 立即引入 Kafka/Temporal | 可靠事件/长流程 | 超出阶段 4，运维复杂 | 延后 |
| 供应商专用 SDK | 功能全 | 锁定、凭证面扩大 | 通用 contract 优先 |

## 后果

- 正向：采集与 API 隔离、任务可重试、租户上下文/RLS 显式、来源可审计。
- 负向：DB 队列不是完整工作流；worker 与 API 需要同一非超级用户角色策略；供应商
  接入需要实现/适配既有 contract。
- 风险：worker 是受控网络发起方，必须部署在受控 egress；DB 队列吞吐受限，阶段 7/8
  需要 Temporal/Kafka 和配额治理。
- 运维：需要监控队列积压、lease 回收、失败率、域名速率与存储增长。

## 验证

单元/集成测试覆盖：搜索 Provider contract/凭证、任务入队幂等、worker 认领/成功/失败/
retry、来源快照与内容去重、跨租户任务访问拒绝、来源删除传播、审计事件、域名速率
限制；测试不依赖真实搜索供应商。
