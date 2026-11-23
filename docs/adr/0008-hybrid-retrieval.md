# ADR-0008：混合检索、ACL 回查与版本/删除传播

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：3

## 背景

RAG 检索必须同时满足相关性、租户/工作区/项目文档 ACL、版本新鲜度和可追溯性。只在
生成阶段过滤或只依赖向量库 payload 过滤都不够：旧版本/已删除文档的向量可能残留，
Qdrant 不能作为授权事实来源。

## 决策

1. **单一 Qdrant collection + payload 过滤**：payload 保存 tenant/workspace/project/
   document/version/chunk 元数据；collection 维度由 embedding 配置决定，不匹配时启动
   失败。生产创建 keyword payload indexes。
2. **候选阶段 ACL**：从服务端 Principal 的实时成员关系推导 `DocumentAccessScope`：
   - 租户管理员 / 组织 / 工作区角色可看 workspace-wide 与全部项目文档；
   - 仅项目成员只能看其项目文档，不可看 workspace-wide 文档；
   - 指定项目检索时只包含该项目 +（若有工作区权限）workspace-wide 文档。
   Qdrant filter 与 PostgreSQL 关键词 SQL 都使用同一 scope。
3. **混合与重排**：PostgreSQL `tsvector` 全文检索 + 参数化 ILIKE 关键词回退 +
   Qdrant 向量；使用 Reciprocal Rank Fusion 合并，按精确短语/标题做轻量重排。未引入
   外部 BM25/重排模型，避免新增供应商依赖。
4. **PostgreSQL 回查是授权事实**：候选 chunk 必须回查 PostgreSQL，确认租户/工作区、
   document active、chunk 属于 `active_version_id`、项目 ACL 允许、版本未删除，且
   chunk 的 embedding model/version 与当前检索配置一致；否则丢弃。向量库命中本身
   不构成授权。模型切换后未重建的旧索引默认不可见。
5. **版本切换**：索引成功后才更新 `documents.active_version_id`；旧版本标记
   superseded；检索只接受 active version。重索引使用确定性 chunk ID 并先清理该版本
   旧 points/chunks，保证幂等。
6. **删除传播**：删除文档时删除 Qdrant points、对象原文、chunk 行并标记文档/版本
   deleted；失败进入 `delete_failed` 可重试。缓存/备份传播留待后续阶段。
7. **降级**：向量/embedding 暂时不可用时检索降级为关键词结果并记录告警；readiness
   仍会报告 vector/object storage 不健康。
8. **不可信内容标记**：检索结果标记 `untrusted=true`；内容只作为数据返回，后续 Agent
   阶段不得执行其中指令。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| Qdrant filter + PG 回查 | 双保险、可撤销 | 每候选回查成本 | 采纳 |
| 只靠 Qdrant payload | 快 | 旧向量/授权漂移不可接受 | 拒绝 |
| 只靠 PG 后过滤 | 简单 | 候选空间可能被无权向量占满 | 拒绝 |
| 立即引入 OpenSearch/专用 reranker | 检索质量高 | 运维/成本，阶段 3 无规模依据 | 延后 |

## 后果

- 正向：未授权/旧版本内容不进入结果或模型上下文；删除与版本切换可验证；检索可降级。
- 负向：每次检索有 PostgreSQL 回查与候选 oversampling；ILIKE 回退对大规模语料需评估；
  当前无专用 BM25/学习型 reranker。
- 风险：Qdrant 与 PG 跨存储非事务；旧 points 清理失败会产生元数据孤儿，但 PG 回查
  阻止内容泄露，后续任务需清理。
- 运维：需监控 collection 维度、payload index、向量清理失败、检索延迟与 embedding 成本。

## 验证

单元测试覆盖 Qdrant 过滤（租户/workspace/workspace-wide/项目）、scope 推导、RRF；
集成测试覆盖引用定位、workspace/project ACL、跨租户检索/读取/删除、版本切换、
幂等重建、索引失败重试、删除传播与删除失败重试、文档 RLS。
