# ADR-0004：OIDC 资源服务器边界与租户 claim

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：2

## 背景

企业客户使用 OIDC/SAML SSO。后端需要认证调用者并把请求绑定到租户，但不能把
JWT 中的角色、权限或租户声明直接视为资源授权结论。浏览器登录流程、SAML XML
签名与 MFA 策略由企业身份平台负责；本项目应保持可替换的身份供应商边界。

## 决策

1. **后端只做资源服务器**：校验 `Authorization: Bearer <JWT>` 的签名、`iss`、
   `aud`、`exp`/`iat`，只允许配置的非对称算法（RS/ES/PS），拒绝 `HS*`/`none`。
2. **JWKS**：通过 `OIDC_JWKS_URI` 获取签名密钥并缓存；不共享对称密钥。
3. **租户 claim 是查找键**：默认读取签名 claim `tenant_id`（可配置），用于定位
   租户；随后必须从 PostgreSQL 查到 `(tenant_id, iss, sub)` 匹配的已启用用户，
   否则 401。没有用户记录不会自动开户。
4. **角色来自数据库**：`role`、`roles`、`permissions` 等 JWT 声明被忽略；每个请求
   从 `organization_memberships` / `workspace_memberships` /
   `project_memberships` 加载角色。
5. **SAML**：本阶段不解析 SAML XML；由企业身份网关换取 OIDC/JWT，或后续实现同一
   `TokenVerifier` 接口。XML 签名/规范化逻辑不进入核心服务。
6. **失败关闭**：OIDC 未配置时 `/api/v1/**` 返回 401；production 启动要求
   `OIDC_ENABLED=true` 且 issuer/JWKS 为 https。
7. **用户生命周期**：首个租户管理员由受控 CLI 创建；后续用户由 tenant_admin 通过
   审计 API 预创建，不在登录时自动开户。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 资源服务器 + 数据库成员角色 | 授权可审计、可撤销 | 每请求查询成员关系 | 采纳 |
| 信任 JWT roles/permissions | 快 | 声明过期/提权、无法细粒度撤销 | 拒绝 |
| 后端 OAuth 授权码 + 会话 | 后端控制登录 | 需要会话/CSRF/多前端适配，非本阶段 | 延后 |
| 自动开户 | 上手快 | 任意 IdP 用户可进入租户 | 拒绝 |

## 后果

- 正向：OIDC/SAML 供应商可替换；权限即时可撤销；JWT 泄露不自动带来角色。
- 负向：每个请求有用户/成员查询；IdP 配置和 claim 约定必须与部署方对齐。
- 风险：租户 claim 由 IdP 签发，若 IdP 配置错误会把用户指向错误租户；用户记录
  与成员关系检查是最终保护。
- 遗留：多 IdP/多 issuer、SAML 原生适配、SCIM、MFA 策略未实现。

## 验证

- 单元测试覆盖签名/issuer/audience/过期/算法/租户 claim/超长 token。
- 集成测试覆盖伪造签名、租户 claim 与用户不匹配、JWT `role=tenant_admin` claim
  被忽略、无 token 401、健康探针公开。
