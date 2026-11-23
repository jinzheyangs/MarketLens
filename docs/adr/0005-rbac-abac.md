# ADR-0005：RBAC + 必要 ABAC 的角色与继承模型

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：2

## 背景

组织、工作区、项目有不同成员与职责。仅有租户级角色无法表达项目级最小权限；
仅靠前端或模型提示词也无法防止越权。需要可测试、可审计、默认为拒绝的服务端
授权。

## 决策

1. **租户级角色**：`tenant_admin` 管理租户（创建组织、管理用户、读取审计）；
   `member` 没有隐式资源权限。
2. **作用域角色**：organization / workspace / project 各自有
   `admin` / `editor` / `viewer`。
3. **向下继承**：组织角色作用于其工作区/项目；工作区角色作用于其项目；子级角色
   不向上授予写权限。祖先只读可由子级成员关系推导（用于导航），但不授予父级写权限。
4. **权限映射**：读取要求 viewer；创建/更新要求 editor；删除、成员管理、组织更新、
   工作区/项目状态变更、项目密级变更要求 admin。租户级权限（用户管理、审计读取）
   仅 `tenant_admin`。
5. **ABAC**：
   - organization suspended：非 admin 写操作拒绝；
   - workspace/project archived：非 admin 写操作拒绝；
   - project confidential：写操作要求有效 admin；
   - 工作区/项目状态与项目密级变更要求 admin，避免 editor 通过归档/密级变更造成
     拒绝服务；
   - tenant suspended：认证后的 API 请求拒绝（人工/CLI 恢复）。
6. **最后管理员保护**：不得撤销或降级组织最后一个 active admin，返回 409，避免
   组织失管。
7. **拒绝审计**：`AuthorizationService` 在返回 403 前用独立事务写入
   `authorization.denied`，包含权限、资源类型、原因和请求 ID（不含敏感内容）。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 作用域角色 + 继承 + ABAC | 表达力、可审计、可测试 | 逻辑略复杂 | 采纳 |
| 单一 tenant_admin/member | 简单 | 无法最小权限 | 拒绝 |
| 仅项目级 ACL | 细 | 组织/工作区管理无处表达、查询复杂 | 拒绝 |
| 模型/前端判断权限 | 开发快 | 可绕过，不可审计 | 拒绝 |

## 后果

- 正向：权限决策集中在服务端；角色变更即时生效；拒绝可追踪。
- 负向：每次授权需要加载资源及成员关系；角色矩阵需文档与回归测试。
- 风险：ABAC 对 archived/confidential 要求 admin，可能阻止 editor 自恢复；
  由 admin 执行恢复是刻意设计。
- 遗留：自定义角色、资源级 ABAC 属性、审批流与临时授权不在本阶段。

## 验证

- 单元测试覆盖租户管理员、组织/工作区/项目角色、继承、只读祖先、无成员、
  confidential/archived/suspended、拒绝审计。
- 集成测试覆盖 viewer 403、editor 创建、JWT claim 提权失败、最后管理员 409、
  ABAC 更新拒绝/允许。
