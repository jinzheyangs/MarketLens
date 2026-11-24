# Observability and SLOs

## Metrics

The API exposes Prometheus metrics only when `METRICS_ENABLED=true`. In
production a `METRICS_BEARER_TOKEN` of at least 24 characters is required.
Metrics are mounted outside the OIDC API prefix and use templated route paths,
never tenant/user IDs or raw URLs.

| Metric | Type | Use |
| --- | --- | --- |
| `marketlens_http_requests_total{method,route,status}` | counter | traffic, 5xx ratio |
| `marketlens_http_request_duration_seconds{method,route}` | histogram | p95/p99 latency |
| `marketlens_http_requests_in_progress` | gauge | saturation / shutdown leaks |
| `marketlens_http_server_errors_total{method,route}` | counter | 5xx attribution |
| `marketlens_app_info{service,version,environment}` | gauge | deployment tracking |

The Helm chart includes a `ServiceMonitor` that authenticates with the metrics
bearer token.

## Suggested SLOs

These are starting points; the deploying organization must approve and measure
them:

| SLO | Target | Measurement |
| --- | --- | --- |
| API availability | 99.9% over 30 days | `1 - 5xx/total` |
| Read/write latency | p95 < 500 ms, p99 < 1 s | request histogram |
| Error budget burn | page at 14.4x burn over 10m | `prometheus-rules.yaml` |

`prometheus-rules.yaml` contains recording rules and alerts for availability
burn, latency, missing API scrapes, worker replica availability, and failed
migration Jobs. `grafana-dashboard.json` visualizes the same signals.

## Logs and traces

- Application logs are structured JSON and include request IDs and route
  templates. Authorization headers, request/response bodies, passwords, and
  secrets are never logged by application middleware.
- Set `OTEL_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT` to export traces and
  metrics over OTLP/HTTP. Sampling is controlled by
  `OTEL_TRACES_SAMPLER_RATIO`; the metrics endpoint is derived as `/v1/metrics`.
- Set `OTEL_LOGS_ENABLED=true` to export structured logs over OTLP/HTTP
  (`/v1/logs`). Structured `context` fields are redacted before export; exception
  text should still be scrubbed by the collector/backend policy.
- `otel-collector.yaml` demonstrates batching, memory limiting, and deletion of
  authorization headers, cookies, SQL statements, and query strings before
  export.

## Gaps

- Workers do not expose their own `/metrics` endpoint; worker health is inferred
  from Kubernetes Deployment status and logs. Add per-worker metrics before
  relying on queue-depth SLOs.
- Dashboard/rules were not executed against a live Prometheus/Thanos stack in
  this repository. Validate them in a staging observability stack.
- SLO targets and error-budget policy require product/operations sign-off.
