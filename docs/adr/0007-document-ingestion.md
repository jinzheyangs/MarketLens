# ADR-0007：对象存储与不可信文档摄取

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：3

## 背景

企业资料包括 PDF/Markdown/文本等不受信任文件。上传内容可能包含畸形结构、超大
响应、路径穿越文件名、压缩/解析炸弹、恶意指令或敏感信息。摄取还需要可恢复状态、
版本、引用定位和删除传播，并且不能把原始文件或解析内容跨租户暴露。

## 决策

1. **对象存储抽象**：`ObjectStorage` 接口提供本地 S3/MinIO 与生产 S3 适配器；
   生产校验要求 S3、TLS 与显式桶；本地允许 MinIO path-style 与自动建桶。对象 key
   全部由服务端生成（tenant/workspace/document/version），客户端文件名只作为展示
   元数据并清洗控制字符/路径分隔符。
2. **格式与边界**：仅允许 `.txt`/`.md`/`.markdown`/`.pdf`；校验扩展名、content-type、
   PDF 魔数、大小、UTF-8/NUL、PDF 页数、解析字符数与 chunk 数。解析在受限线程执行，
   不执行文件中任何指令。
3. **状态机**：版本 `uploaded → processing → indexed/failed`，失败可由人工/API 重试；
   `failed` 保留稳定 error_code 与安全错误文本，不保存堆栈。`processing`/`deleting`
   使用 updated_at 租约（`DOCUMENT_STATE_LEASE_SECONDS`），崩溃后过期可重试；未过期
   的并发请求返回 409。请求内同步处理是本阶段限制，后续迁移到持久工作流。
4. **版本不可变**：每次上传生成新的版本记录与对象 key；只有成功索引后才切换
   `documents.active_version_id`，旧版本标记 `superseded`。chunk 保存文档版本、页码/
   段落/offset、内容哈希与 embedding model/version。
5. **事务边界**：索引状态切换使用独立短事务与行锁；对象/向量操作在事务外，失败写入
   `failed`。重试先删除该版本旧向量/chunk（确定性 chunk ID）再重建，保证幂等。
6. **审计与日志**：记录 document.uploaded/indexed/index_failed/deleted/delete_failed；
   不记录文件名、正文、token 或对象内容。
7. **删除**：删除文档先进入 `deleting`，删除 Qdrant points、原始对象、chunk 行并标记
   版本/文档 deleted；任一步失败进入 `delete_failed` 可重试。搜索/读取只接受 active
   文档，失败状态不会泄露内容。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 后台直传 + 服务端校验/解析 | 可控、可审计 | 请求内同步处理成本 | 阶段 3 采纳 |
| 仅信任扩展名 | 简单 | 可伪造、解析风险 | 拒绝 |
| 原始文件名作为对象路径 | 可读 | 路径穿越/覆盖风险 | 拒绝 |
| 立即引入 Temporal/worker | 长任务可靠 | 属阶段 7，提前引入复杂 | 延后 |

## 后果

- 正向：租户隔离、可重试状态、版本与引用元数据完整；上传面默认 fail closed。
- 负向：索引是在请求内同步执行，大文档可能触及请求超时；真实 Qdrant/S3 需要部署
  验证；OCR/DOCX/表格未覆盖。
- 风险：Qdrant 与 PostgreSQL 跨存储操作不是分布式事务；通过 PG 回查、确定性 ID、
  可重试状态降低影响，阶段 7 需 outbox/工作流完善。
- 运维：需要对象存储生命周期/加密/备份策略、Qdrant 容量与快照、worker 化改造。

## 验证

单元测试覆盖格式/大小/魔数/NUL、文本与 PDF 解析、分块 offset、本地存储路径穿越；
集成测试覆盖上传-索引-检索、跨租户、版本切换、幂等重建、索引失败重试、删除传播、
删除失败重试、RLS 与迁移往返。
