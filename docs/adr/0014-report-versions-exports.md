# ADR-0014：不可变报告版本、人工审批与导出任务

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：6

## 背景

审查后的报告需要可追溯、可审批、可下载，且不能因为重新研究/重新生成覆盖既有已批准
内容。报告包含来自不可信来源的引用，导出对象也需要租户隔离、大小限制、审计和失败恢复。

## 决策

1. **报告与版本分离**：`reports` 每个 root run 一行，保存当前版状态/current_version；
   `report_versions` 保存不可变内容（Markdown、结构化 JSON、来源列表、citation index）、
   review 关联、版本号和覆盖 Markdown+结构化 JSON 的 SHA-256 checksum。重新 review 只新增版本，不修改旧版本内容。
2. **确定性生成**：报告由审查后的结构化 claim/evidence/source 生成，包含执行摘要、方法、
   发现、风险/限制、冲突与缺口、证据链接、来源列表和审查汇总。没有模型叙事的“自评”
   步骤，避免生成与证据不一致的批准文本。
3. **审批门禁**：只有当前 draft version 可以 approve/reject；approve 要求
   `report:approve`（workspace/project admin），拒绝必须记录原因。审批/拒绝写审计并
   记录版本号、checksum、actor。审批前再次校验 Markdown+结构化 JSON checksum。
4. **导出任务**：只有 approved version 可创建 export job；支持 Markdown 与 DOCX。
   job 使用 PostgreSQL lease、最大尝试、退避、worker_id fencing、tenant 上下文；
   导出对象 key 由服务端生成并写入对象存储，失败可重试，终态失败写审计。
5. **下载控制**：下载 API 先重新授权 root run/report，只允许 succeeded job；返回服务端
   生成的 Content-Disposition 文件名并设置 `Cache-Control: no-store`，不暴露对象 key。
6. **格式边界**：DOCX 由 python-docx 生成；PDF、表格/图表、Word 模板、数字签名和外部
   DMS 集成延后。导出只表示“已批准版本可用”，不宣称格式在所有 Office 版本完全兼容。
7. **ACL**：报告继承 root run 的 workspace/project ACL；不逐条重鉴权原始来源。原来源
   删除不会删除历史报告/审计，但后续 review 会把 citation 标为 invalid。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 不可变版本 + 审批 + 导出队列 | 可追溯、可重试、职责分离 | 表/worker 增加 | 采纳 |
| 覆盖同一报告行 | 简单 | 审批/审计不可追溯 | 拒绝 |
| 同步生成下载 | 无 worker | 请求超时、失败不可恢复 | 拒绝 |
| 所有格式一次实现 | 功能全 | PDF 字体/版式/DMS 复杂，风险高 | 延后 |
| 允许 draft 导出 | 操作快 | 未审批内容外泄 | 拒绝 |

## 后果

- 正面：审批与生成分离；旧版本和审批记录不可变；导出失败可重试；下载受租户/角色约束。
- 负面：需要 review/export worker；报告内容是确定性模板，叙事可读性有限；PDF/表格
  尚未交付。
- 安全：审批/导出均为服务端授权；对象存储不暴露给客户端；来源 ACL 的引用级细化仍需
  后续阶段；报告版本 checksum 提供基本内容完整性。
- 运维：需监控 review/export 队列、对象存储增长、导出失败和审批积压；S3 实际服务未在
  本机验证。

## 验证

单元测试覆盖 Markdown/DOCX 渲染。集成测试覆盖 report review、版本 1/2、审批权限、
checksum 审批、Markdown 导出 job/下载、审计事件、followup 降级报告、跨租户 404 和
报告表 RLS；迁移执行 upgrade/downgrade/upgrade 与 `alembic check`。
