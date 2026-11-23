# ADR-0002：租户隔离底座与 fail-closed 写入守卫

- 状态：已接受（完整授权待阶段 2）
- 日期：2026-11-23
- 关联阶段：1、2

## 背景

蓝图要求租户隔离覆盖 API、SQL、对象存储、向量检索、缓存、队列、日志和导出。阶段 1
必须为后续业务表建立不可绕过的数据结构与写入默认值，但不能用未认证的请求头冒充授权，
也不能提前实现阶段 2 的 RBAC/ABAC 与审计。

## 决策

1. **数据模型**：所有业务表继承 `TenantScopedMixin`，`tenant_id UUID NOT NULL` 且带索引。
   当前仅创建 `tenants` 根注册表，作为未来外键锚点；组织/工作区/项目在阶段 2 建立。
2. **上下文**：`tenant_id` 存于 `ContextVar`，只允许服务端可信代码通过
   `tenant_context()` / `set_tenant_id()` 绑定。**客户端 header 不参与绑定。**
3. **写入守卫**：SQLAlchemy `before_flush` 在无租户上下文时拒绝租户表写入；新行缺失
   `tenant_id` 时自动绑定；跨租户创建/修改/删除抛出 `TenantIsolationError`。
4. **读取辅助**：`tenant_scoped_select()` 强制追加 `tenant_id = require_tenant_id()`，
   无上下文直接失败，不返回全量数据。
5. **错误模型**：缺少租户上下文视为服务端集成错误（500，不泄露内部细节）；跨租户
   越界返回 403。

## 明确不在本阶段

- OIDC/SAML、用户/成员/角色、资源级 ABAC、审计事件。
- PostgreSQL RLS 与 `SET LOCAL app.tenant_id` 会话变量。
- 对象存储路径、Qdrant payload filter、缓存 key、消息队列的租户隔离。
- 对原生 SQL、批量 UPDATE/DELETE 的强制拦截。

上述内容必须在阶段 2/3/4/7 完成，并补充跨租户负向测试。当前机制**不能**被视为完整
隔离或合规证明。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 仅加 tenant_id 字段 | 改动小 | 容易漏写过滤/写入 | 不足 |
| 客户端头传租户 | 简单 | 可伪造，违反服务端授权 | 拒绝 |
| ORM 写入守卫 + 查询辅助 | fail closed、可测试 | 不覆盖原生 SQL | 阶段 1 采纳 |
| 立即 RLS | 数据库级强制 | 需认证上下文、连接池/事务变量设计，属阶段 2 | 延后 |

## 后果

- 正面：新增业务表默认获得租户写入保护；测试可直接验证拒绝路径。
- 负面：所有写入必须在已绑定租户的上下文中进行；系统任务也需显式绑定，不能静默全局写。
- 风险：ORM 事件不覆盖 Core `update()/delete()` 与原生 SQL；阶段 2 必须补 RLS/查询审计。
- 运维：连接池 `statement_timeout`/`idle_in_transaction_session_timeout` 限制悬挂事务。

## 验证

`tests/unit/test_tenant_scope.py`、`tests/integration/test_database_isolation.py`：

- 无上下文写入拒绝；
- 新行自动绑定；
- 跨租户插入拒绝；
- 查询辅助只返回当前租户行；
- 无上下文查询辅助失败；
- 根 `tenants` 注册表不被守卫误拦截。
