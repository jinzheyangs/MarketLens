# 证据审查与报告交付操作说明（阶段 6）

## 生命周期

1. 阶段 5 research run 进入 `completed`/`partial` 后，`REPORT_ENABLED=true` 且
   `REPORT_AUTO_REVIEW=true` 时自动创建 `research_reviews` pending 行。
2. `python -m app.agent.worker` 处理：
   - research run；
   - pending/awaiting review；
   - report export job。
3. Reviewer 将 finding 派生为 source/evidence/claim/citation，并逐条校验：
   - 来源/版本/chunk/snapshot 仍存在；
   - quote 仍出现在来源文本；
   - 词法支持分数、freshness、独立来源数、冲突极性。
4. 存在 gaps 且未达到补查上限时，创建 linked followup research run，review 进入
   `awaiting_followup`；followup 结束后自动回到 review。
5. 达到上限或无需补查时生成 draft report version，review 进入 `completed`。
6. 人工审批：
   - `POST/.../report/versions/{version_id}/approve`，需要 report admin；
   - 或 `reject` 并填写原因；
   - 审批前校验 Markdown SHA-256 checksum。
7. 只有 approved version 可以创建 export job；worker 生成 Markdown/DOCX，写入对象存储；
   客户端通过 download API 下载。

手动重启审查：

```http
POST /api/v1/workspaces/{workspace_id}/research-runs/{root_run_id}/review
```

`REPORT_AUTO_REVIEW=false` 时首次审查必须通过该 API 启动；后续 followup 仍会自动唤醒。
自动排队只针对部署后完成的新 run；历史 run 需要显式调用该 API。

## 查询

```http
GET /api/v1/workspaces/{workspace_id}/research-runs/{root_run_id}/review
GET /api/v1/workspaces/{workspace_id}/research-runs/{root_run_id}/review/claims
GET /api/v1/workspaces/{workspace_id}/research-runs/{root_run_id}/report
GET /api/v1/workspaces/{workspace_id}/research-runs/{root_run_id}/report/versions
GET /api/v1/workspaces/{workspace_id}/research-runs/{root_run_id}/report/versions/{version_id}
```

可以通过一个 followup run id 访问同一 root report；API 会解析 `root_run_id` 后重新授权。

## 审批与导出

```http
POST .../report/versions/{version_id}/approve
POST .../report/versions/{version_id}/reject        {"reason": "..."}
POST .../report/versions/{version_id}/exports       {"format": "markdown" | "docx"}
GET  .../report/exports
GET  .../report/exports/{job_id}
GET  .../report/exports/{job_id}/download
```

- 审批：`report:approve`（workspace/project admin）
- 导出：`report:export`（editor+）
- 读取/下载：`report:read`

审批、导出和下载都绑定 root run 的 workspace/project 作用域，不使用前端过滤。

## 失败与重试

- review/export 使用 PostgreSQL lease + max attempts + 指数退避；
- 权限丢失、root run 非终态、来源删除导致不可验证等没有安全意义的错误不会无限重试；
- 导出对象写入成功但 DB 收尾失败时，同一 job 重试会覆盖同一服务端生成对象 key；
- 审查失败不会伪造成功报告；已有 approved version 不会被后续 review 覆盖。

## 报告格式

Markdown 版本包含：

- Executive Summary；
- Methodology；
- Findings（supported/partial）；
- Risks and Limitations；
- Conflicts and Gaps；
- Evidence Links；
- Sources；
- Review Summary。

DOCX 由同一 structured JSON 生成。PDF/表格导出、数字签名、Office 全版本兼容和外部
DMS 集成尚未实现；生产需由部署方验证模板、字体和对象存储生命周期策略。
