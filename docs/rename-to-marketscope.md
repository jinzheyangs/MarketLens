# 从旧名称迁移到 MarketScope

本次重命名覆盖品牌、Python 发行包、默认数据库/S3/Kafka/Temporal 标识、Prometheus
指标、Helm chart 路径、通知头、日志和文档。Python 业务包名仍然是 `app`，不需要改
import 路径。

## 1. 标识变化

| 类别 | 旧值 | 新值 |
| --- | --- | --- |
| 品牌显示 | MarketLens | MarketScope |
| Python 发行包 | market-intel-backend | marketscope-backend |
| CLI | market-intel | marketscope |
| Compose 项目名 | market-intel | marketscope |
| 镜像 | market-intel-api | marketscope-api |
| 数据库/用户默认值 | market_intel(_test) | marketscope(_test) |
| S3 默认桶 | marketlens-documents | marketscope-documents |
| Kafka topic | marketlens.domain-events | marketscope.domain-events |
| Kafka consumer group | marketlens-backend | marketscope-backend |
| Temporal task queue | marketlens-research | marketscope-research |
| Prometheus 指标前缀 | marketlens_* | marketscope_* |
| 通知头 | X-MarketLens-* | X-MarketScope-* |
| Helm chart | deploy/helm/marketlens | deploy/helm/marketscope |

## 2. 就地升级已有环境

### 数据库

表结构没有变化，不需要迁移数据。已有数据库可以继续使用，只要在环境中显式设置：

```bash
POSTGRES_USER=<旧用户>
POSTGRES_DB=<旧库>
DATABASE_URL=postgresql+asyncpg://<旧用户>:<密码>@<host>:5432/<旧库>
```

如果要切换到新的默认名称：

```sql
CREATE ROLE marketscope LOGIN PASSWORD '<新密码>';
CREATE DATABASE marketscope OWNER marketscope;
CREATE DATABASE marketscope_test OWNER marketscope;
```

然后使用新 `DATABASE_URL` 运行 `alembic upgrade head`。注意本地测试默认库名变为
`marketscope_test`；旧测试库不会自动迁移。

### Kafka

默认 topic 和 consumer group 已变化。升级时二选一：

1. 在环境中显式保留旧值：
   ```bash
   EVENT_TOPIC=marketlens.domain-events
   EVENT_DEAD_LETTER_TOPIC=marketlens.domain-events.dead-letter
   EVENT_CONSUMER_GROUP=marketlens-backend
   KAFKA_CLIENT_ID=marketlens-events
   ```
2. 创建新 topic/group、授予 ACL，并让 consumer 从 earliest/latest 按业务要求启动。
   `processed_events` 仍按 tenant + consumer + event_id 幂等；换 consumer group 不会
   自动重放已处理事件。

### Temporal

已有 workflow 的 task queue 使用旧值。切换新队列前：

- 排空旧 worker；
- 确认旧 workflow 已结束或由运维 reset/terminate；
- 新 worker 配置 `TEMPORAL_TASK_QUEUE=marketscope-research`。

### S3/对象存储

已有对象仍在旧 bucket。新部署需显式设置：

```bash
S3_BUCKET=<旧桶>
```

或创建新桶并迁移对象。不要只改代码默认值而忘记迁移历史对象。

### Prometheus/Grafana

指标名从 `marketlens_*` 变为 `marketscope_*`。更新：

- recording rules；
- alert rules；
- dashboard queries；
- ServiceMonitor 无影响；
- 历史时间序列不会自动改名。

### 通知接收方

webhook/Slack/SMTP 接收方需要更新：

- 签名头 `X-MarketScope-Signature`；
- delivery/idempotency 头 `X-MarketScope-*`；
- 邮件标题中的 `[MarketScope]`；
- 邮件 Message-ID 域名 `marketscope.local`。

### Helm

新 chart 路径：

```bash
helm upgrade --install marketscope deploy/helm/marketscope -n marketscope
```

不要直接对旧 release 执行 upgrade 后混用两个 release 名；先确认旧 release 的
Service/Ingress/PVC/ExternalSecret 清理策略。Kubernetes Secret 名称、namespace、
ExternalSecret target 都需要按环境更新。

### Qdrant

RAG chunk 的确定性点 ID 前缀从 `marketlens:` 改为 `marketscope:`。已有 collection
不会自动改 ID。若要彻底切换：

- 新建 collection 并重新索引；或
- 保留旧 collection 直到迁移完成，再删除旧数据。

### Python 安装

```bash
pip install -e ".[dev]"
```

如果本地环境仍显示旧发行包，执行 `pip uninstall market-intel-backend` 后再安装。

## 3. 本地目录和 GitHub 仓库

本次代码重命名不自动修改本地目录 `D:\endd` 或 GitHub 仓库名。需要时手动执行：

```powershell
# 关闭使用该目录的进程后，在父目录执行
Rename-Item D:\endd D:\MarketScope
```

GitHub 仓库可以在 Settings → Repository name 改为 `MarketScope`，然后更新：

```bash
git remote set-url origin git@github.com:<owner>/MarketScope.git
```

仓库改名会影响外部链接、CI badge 和 webhook，需同步通知团队。

## 4. 验证清单

```bash
alembic upgrade head
pytest -q
helm lint deploy/helm/marketscope
terraform fmt -check -recursive
```

- [ ] 应用连接的新/旧数据库正确
- [ ] Kafka topic/consumer group ACL 正确
- [ ] Temporal task queue 与 worker 一致
- [ ] S3 bucket 可读写
- [ ] Prometheus 指标名和告警表达式已更新
- [ ] 通知接收方已更新签名头
- [ ] Helm release/Secret/Ingress 无重复资源
- [ ] Qdrant 重建/兼容策略已确认
