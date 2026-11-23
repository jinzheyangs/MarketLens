# ADR-0013：独立 claim/evidence/source/citation 与确定性 Reviewer

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：6

## 背景

阶段 5 的 finding 把 claim、quote 和来源信息放在同一行，足以生成研究草稿，但不适合
报告复核：同一结论可能由多条证据支持，同一来源可能有多个版本/快照，引用必须独立
校验；模型不能自评“已支持”来替代来源事实。阶段 6 还需要有限补查和可审计的降级表达。

## 决策

1. **规范化实体**：在 root research run 下派生
   - `research_sources`：文档版本或外部 WebSource 快照；
   - `research_evidence`：可定位 quote（chunk/page/offset 或 snapshot）；
   - `research_claims`：规范化结论、scope、原始局限和审查状态；
   - `research_citations`：claim→evidence 关系、支持分和审查原因。
   所有表带 tenant、root run、RLS 与复合外键；派生幂等。
2. **确定性 Reviewer**：强制校验 citation/source/version/chunk/snapshot 仍存在、quote
   仍在来源文本中，并计算词法支持分、freshness、独立来源数。claim 状态分为
   supported/partially_supported/insufficient/conflicted/invalid；任何无有效引用的 claim
   不得报告为 supported。
3. **冲突检查**：对共享显著词且极性相反的 claim 做保守词法标记。该规则可能漏报/误报，
   必须在报告中保留人工复核和限制说明；不作为自动事实核查或合规结论。
4. **模型输出不可自评**：阶段 5 模型只产生原始 finding；Reviewer 不使用同一模型给
   claim 打“已支持”。后续若引入语义 reviewer，必须是独立可审计的 provider，且仍以
   强制 citation 校验为前置条件。
5. **有限补查**：对 invalid/insufficient/conflicted 和独立来源不足的 partial 生成最多
   3 个问题，创建 linked `run_kind=followup` run；root review 等待 followup 完成后再
   审查所有 lineage findings。轮数受 `REPORT_MAX_FOLLOWUP_ROUNDS` 限制；达到上限后生成
   带 gaps/conflicts 的降级报告，不继续循环。
6. **状态可恢复**：review 使用 PostgreSQL 状态机、lease、重试上限和 tenant 上下文；
   每个 followup run 只在 review 记录的 round 增加一次，防止重复补查。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 独立实体 + 规则 Reviewer | 可验证、可审计、无模型自评 | 语义能力有限 | 采纳 |
| 复用 finding 行 | 改动少 | 无法表达多证据/多来源/复核历史 | 拒绝 |
| 让生成 claim 的模型自评 | 便宜、语义好 | 自评偏差、不可审计 | 拒绝 |
| 不限制补查 | 可能提高覆盖 | 成本/无限循环/状态复杂 | 拒绝 |
| 无补查直接报告 | 简单 | 证据不足时无恢复路径 | 不足 |

## 后果

- 正面：claim 与证据关系明确；citation 可逐条验证；报告能明确 downgrade；补查有硬边界；
  review/followup 可恢复。
- 负面：词法支持/冲突只是启发式；派生和规则审查增加 DB 写放大；语义质量需后续独立
  reviewer 提升。
- 安全与隔离：实体、review、followup 全部 tenant/root-run 作用域，RLS 与 API 授权双重
  约束；来源必须当前可读取，删除的来源会使 citation invalid。
- 后续：阶段 7 用 Temporal 管理跨服务 review/followup 生命周期；阶段 8/9 增加引用级
  ACL、配额、成本归集和更强的语义审查。

## 验证

单元测试覆盖支持分、极性冲突、freshness、quote 存在性和导出。集成测试覆盖自动 review、
实体派生、source 删除后 invalid、有限 followup、降级报告、claim/citation API 和 RLS。
