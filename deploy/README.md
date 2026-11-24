# MarketScope deployment assets

These are production-oriented **samples and operational controls**, not a claim
that a particular cluster, cloud account, or disaster-recovery objective has
been validated. Every environment must be planned, applied, and drilled by the
deploying organization.

| Directory | Purpose |
| --- | --- |
| `helm/marketscope` | Helm chart for API, collector/agent/events/monitor workers, migration Job, NetworkPolicy, HPA/PDB, ServiceMonitor, ExternalSecret |
| `terraform` | AWS sample: encrypted Multi-AZ RDS PostgreSQL, S3 with KMS/versioning/public-access blocks, Secrets Manager |
| `observability` | Prometheus SLO recording/alert rules, Grafana dashboard, OpenTelemetry Collector pipeline |
| `scripts` | PostgreSQL backup/verify/restore scripts with checksums and explicit confirmation |
| `load` | k6 smoke/load scenario for an approved drill window |

## Required runtime secret

Create a Kubernetes Secret (or ExternalSecret) with at least:

| Key | Notes |
| --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://...`; TLS parameters per database provider |
| `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URI` | Identity provider resource-server settings |
| `ALLOWED_HOSTS` | Exact API hostnames; production rejects wildcards |
| `NOTIFICATION_ENCRYPTION_KEY` | Fernet key when monitoring is enabled |
| `METRICS_BEARER_TOKEN` | ≥24 random characters when metrics are enabled |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_ENDPOINT_URL` | Production object storage; prefer IRSA/workload identity where supported |
| `KAFKA_SASL_USERNAME`, `KAFKA_SASL_PASSWORD`, `KAFKA_SSL_CAFILE` | Production requires TLS/SASL |
| `AGENT_API_KEY` | Model gateway credential when `AGENT_PROVIDER=http` |
| `WEB_SEARCH_API_KEY` | Search provider credential |
| `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | Optional email notification relay; recipients stay encrypted in the channel config |

Never commit a populated `values.yaml`, tfvars, or Secret. The chart references
an existing Secret by name only.

## Minimum production controls

- Run migrations through the Helm pre-upgrade Job; review logs before rollout.
- Keep the API at two or more replicas with a PodDisruptionBudget.
- Keep database deletion protection and S3 versioning enabled.
- Scrape `/metrics` only inside the cluster or through an authenticated proxy.
- Ship logs/traces through the OpenTelemetry Collector with header/body redaction.
- Run the backup script on a schedule and perform a restore drill at least
  quarterly; record measured RPO/RTO rather than assuming the targets.
- Apply dependency, image, and SBOM scanning in CI and block on critical findings
  per your risk policy.
