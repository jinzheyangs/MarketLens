# ADR-0006：数据库 RLS 与租户复合外键

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：2

## 背景

阶段 1 只有 `tenant_id` 字段、ORM 写入守卫和查询辅助。阶段 2 引入更多业务表和
认证上下文，需要让数据库层也具备租户边界，防止应用层遗漏过滤或跨租户父资源
引用。与此同时，PostgreSQL 超级用户会绕过 RLS，部署角色必须明确。

## 决策

1. **事务级 GUC**：每个 SQLAlchemy Session 在 `after_begin` 时执行
   `set_config('app.tenant_id', :tenant_id, true)`；事务结束自动重置，连接池不会
   残留其他租户的值。
2. **RLS 策略**：所有租户表启用 `ENABLE` 与 `FORCE ROW LEVEL SECURITY`，策略为
   `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`，同时用于
   `USING` 与 `WITH CHECK`。缺少上下文时比较为 NULL，读写 fail closed。
3. **应用层纵深防御**：仓储/查询辅助仍显式追加 `tenant_id`，ORM 写入守卫仍检查
   ContextVar；RLS 不是唯一防线。
4. **复合外键**：workspace→organization、project→workspace、
   membership→scope/user 的 FK 同时包含 `tenant_id`，数据库拒绝跨租户父资源引用。
5. **认证绑定顺序**：验证 JWT 后绑定租户上下文，再查询用户与成员关系；RLS 使
   该查询不可能返回其他租户的数据。
6. **生产角色**：API 连接必须是非超级用户（最好非表 owner 或配合 FORCE owner
   策略）；超级用户/`BYPASSRLS` 会绕过策略。迁移使用单独的管理角色。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 应用过滤 + RLS + 复合 FK | 纵深防御、可测试 | 需要角色/事务变量运维 | 采纳 |
| 仅应用层过滤 | 简单 | 漏写即越权 | 不足 |
| 仅 RLS | 数据库强制 | 应用审计/错误语义仍需过滤 | 不足 |
| 每租户独立 schema/库 | 强隔离 | 迁移与运维成本高 | 延后 |

## 后果

- 正向：跨租户 SQL、错误父资源引用在数据库层被拒绝；连接复用安全。
- 负向：本地 Compose 的超级用户会绕过 RLS，需要测试 `SET ROLE` 或部署非超级用户；
  复杂查询必须保持租户 GUC。
- 风险：直接使用超级用户连接生产库会静默削弱隔离；部署检查必须覆盖。
- 遗留：备份恢复、审计留存、数据删除传播与跨区域灾备属于后续阶段。

## 验证

- 集成测试验证 8 张租户表的策略存在、Session 设置/清理 GUC、非超级用户只能看到
  当前租户、无上下文返回空、跨租户 INSERT 被 RLS 拒绝、跨租户父资源被复合 FK 拒绝。
- 迁移 `upgrade → downgrade → upgrade` 与 `alembic check` 通过。
