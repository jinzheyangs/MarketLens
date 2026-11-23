# ADR-0010：SSRF 防护、快照去重与采集重试

- 状态：已接受
- 日期：2026-11-23
- 关联阶段：4

## 背景

采集服务会访问用户/搜索供应商提供的任意 URL，典型威胁包括 SSRF 访问云元数据或内网、
DNS 重绑定、重定向到私网、超大响应、压缩炸弹、恶意内容类型、robots/来源政策违规、
无限重试和跨租户资源耗尽。外部网页内容本身也不可信。

## 决策

1. **URL 白名单边界**：仅 http/https；默认端口 80/443（production 强制）；拒绝
   userinfo、控制字符、超长 URL 和私网 IP literal；全局域名 allow/block 策略，
   blocked 优先。
2. **DNS 校验 + pinning**：每次导航解析 DNS，拒绝任何非公网答案（混合答案 fail
   closed），连接直接 pin 到已验证 IP，同时保留 Host/TLS SNI 与证书校验，避免校验后
   二次解析造成 DNS 重绑定。重定向逐跳重新执行完整校验，限制层级并检测循环。
3. **响应边界**：请求超时、最大响应字节、最大提取字符、最大重定向；只接受
   text/html、text/plain、application/xhtml+xml；拒绝 Content-Encoding 非 identity；
   HTML 抽取不执行脚本，忽略 script/style/noscript/template/svg/iframe。
4. **robots/来源政策**：可配置 User-Agent；抓取前检查 robots.txt，缓存、支持常见
   Allow/Disallow/Crawl-delay；401/403 拒绝，404 允许，5xx 临时不可用。生产建议保持
   `COLLECTOR_RESPECT_ROBOTS=true`。
5. **并发/速率**：worker 全局并发上限；`collector_domain_limits` 以 PostgreSQL 原子
   upsert 为 tenant+domain 预留起始时隙，跨 worker 共享，Crawl-delay 参与间隔。
6. **快照/去重**：原文写入对象存储；来源按 workspace+canonical URL hash 去重，快照按
   content hash 去重；相同内容不重复写对象。删除来源时删除快照对象/行并写审计。
7. **重试/租约**：任务带最大尝试次数、指数退避、running lease 和过期回收；策略错误
   终止，网络/429/5xx 可重试；API 支持人工 retry。失败只保存稳定 error_code。
8. **测试网络边界**：不依赖真实供应商/公网；使用本地 HTTP server 验证重定向、大小、
   类型、压缩限制；用注入 resolver 验证私网/混合 DNS 拒绝；搜索 Provider 用 Mock。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 校验 URL 后普通 HTTP client | 简单 | DNS 重绑定/重定向绕过 | 拒绝 |
| 解析并 pin IP + 逐跳校验 | 防重绑定 | 自实现 HTTP 子集 | 采纳 |
| 仅黑名单私网 IP | 快 | 绕过/新特殊段风险 | 不足 |
| 不限制大小/压缩 | 兼容好 | DoS/解压炸弹 | 拒绝 |

## 后果

- 正向：采集边界默认拒绝私网/元数据/超大/压缩/非文本；重定向和 DNS 都重新校验；
  速率与重试可恢复。
- 负向：只支持 HTTP/1.1 和无压缩响应；某些站点需 gzip/JS 渲染时无法采集；自实现
  连接层需要安全评审；robots 解析不是完整 RFC。
- 风险：worker 网络出口必须由部署方限制；域名策略为部署级；robots 缓存按进程；
  DNS 线程超时不能强杀底层解析。
- 运维：需监控被拒绝目标、robots 失败、队列积压、域名速率、对象存储增长和清理。

## 验证

单元测试覆盖 URL 校验、私网/云元数据/IPv6 映射拒绝、混合 DNS 答案拒绝、域名策略、
robots 解析/缓存/状态映射、本地 HTTP server 的重定向/大小/类型/压缩、HTML 抽取和
本地对象存储路径安全。集成测试覆盖任务重试、来源快照/去重、跨租户访问拒绝、来源
删除传播和域名速率限制。
