# 真实依赖联调

本仓库的常规测试使用 mock/本地模式；`tests/integration/real` 提供可选的真实服务测试，
只有显式配置端点时才运行，绝不会指向生产数据。

## 环境变量

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://market_intel:test-only@127.0.0.1:55432/market_intel_test'
export QDRANT_TEST_URL='http://127.0.0.1:6333'
export S3_TEST_ENDPOINT_URL='http://127.0.0.1:9090'
export S3_TEST_ACCESS_KEY='test-key'
export S3_TEST_SECRET_KEY='test-secret'
export KAFKA_TEST_BOOTSTRAP_SERVERS='127.0.0.1:9092'
export TEMPORAL_TEST_ADDRESS='127.0.0.1:7233'

pytest -m real tests/integration/real -q
```

每个测试都会创建随机 collection/bucket/topic/tenant/workflow ID，不依赖预置数据。

## 本地启动方式（Docker）

### PostgreSQL

若主机 5432 已被占用，映射到 55432：

```bash
docker run -d --name ml-postgres \
  -e POSTGRES_USER=market_intel \
  -e POSTGRES_PASSWORD=test-only \
  -e POSTGRES_DB=market_intel_test \
  -p 127.0.0.1:55432:5432 postgres:16-alpine
```

### Qdrant

```bash
docker run -d --name ml-qdrant \
  -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
  qdrant/qdrant:v1.12.5
```

生产请固定与 `qdrant-client` 兼容的 server/client 版本。当前 client 1.20 连接 1.12.5
会打印兼容性警告，但基础检索/过滤已验证。

### S3 兼容存储

当前网络对 MinIO 镜像的拉取受限，可使用 Adobe S3Mock 验证 boto3 S3 API：

```bash
docker run -d --name ml-s3 -p 127.0.0.1:9090:9090 adobe/s3mock:latest
```

测试会使用 `s3_auto_create_bucket=true` 创建随机桶。生产必须改用真实 S3/MinIO，
并验证桶权限、TLS、生命周期、加密和跨区域复制。

### Kafka / Redpanda

Compose 里的 Redpanda 默认 advertised address 是 `redpanda:9092`，主机客户端无法解析。
主机联调可使用：

```bash
docker run -d --name ml-redpanda -p 127.0.0.1:9092:9092 \
  redpandadata/redpanda:v24.2.7 \
  redpanda start --overprovisioned --smp 1 --memory 1G --reserve-memory 0M \
  --node-id 0 --check=false \
  --kafka-addr PLAINTEXT://0.0.0.0:9092 \
  --advertise-kafka-addr PLAINTEXT://127.0.0.1:9092
```

### Temporal

Docker 镜像在当前镜像源不可用时，可下载 Temporal CLI dev server：

```bash
temporal server start-dev --ip 127.0.0.1 --port 7233 --http-port 8234
```

`--port 7233` 是 gRPC；`--http-port` 仅为本地 UI/HTTP，避免与已占用端口冲突。

## 覆盖内容

| 测试 | 验证点 |
| --- | --- |
| `test_real_qdrant_tenant_isolation` | 真实 Qdrant collection、payload filter、跨租户隔离 |
| `test_real_s3_object_storage` | 真实 S3 HTTP API：ensure bucket、put/get/delete |
| `test_real_rag_upload_index_search` | API 上传 → S3 → Qdrant 索引 → 搜索返回引用定位 |
| `test_real_kafka_publish_and_consume` | aiokafka 真实 produce/consume、envelope 解析 |
| `test_real_outbox_dispatches_to_kafka` | PostgreSQL outbox → Kafka → DB `published` 状态 |
| `test_real_temporal_workflow` | 真实 Temporal server 执行 workflow + activities |

## 当前本地验证结果

在 Docker Desktop（PostgreSQL 16、Qdrant 1.12.5、S3Mock、Redpanda v24.2.7）和
Temporal CLI 1.1.0 dev server 上执行：

```text
269 passed   # 常规测试，使用真实 Docker PostgreSQL
6 passed     # tests/integration/real
```

完整 API 启动后的 `/health/ready` 同时返回：

```json
{
  "status": "ok",
  "components": {
    "database": {"status": "ok"},
    "object_storage": {"status": "ok"},
    "vector_store": {"status": "ok"}
  }
}
```

## 尚未验证

- 生产 TLS/SASL Kafka、Temporal Cloud/多节点持久化、Qdrant 集群
- 真实 S3/MinIO 的 IAM、KMS、生命周期、跨区域复制
- RDS 主备切换/PITR、Kubernetes 节点驱逐、k6 压测
- 真实模型/搜索供应商、真实 OIDC IdP
