# MarketLens Helm chart

## Install

```bash
kubectl create namespace marketlens
kubectl -n marketlens create secret generic marketlens-secrets \
  --from-literal=DATABASE_URL='postgresql+asyncpg://...' \
  --from-literal=OIDC_ISSUER='https://idp.example.com' \
  --from-literal=OIDC_AUDIENCE='marketlens' \
  --from-literal=OIDC_JWKS_URI='https://idp.example.com/jwks' \
  --from-literal=NOTIFICATION_ENCRYPTION_KEY='<fernet-key>' \
  --from-literal=METRICS_BEARER_TOKEN='<random-24+-chars>'

helm upgrade --install marketlens . \
  -n marketlens \
  --set api.ingress.enabled=true \
  --set api.ingress.hosts[0].host=api.example.com \
  --atomic --timeout 20m
```

The chart does not create stateful dependencies. Point `DATABASE_URL`, S3,
Kafka, Temporal, and Qdrant settings at managed services in the runtime Secret.
Prefer External Secrets/CSI over `kubectl create secret` for production.

## Values to review per environment

- `existingSecret` / `externalSecrets.*`
- `env.ENVIRONMENT`, `env.ALLOWED_HOSTS`, OIDC and provider endpoints
- `image.registry`, `image.repository`, `image.tag`
- `api.replicaCount`, resources, autoscaling, ingress/TLS
- `workers.*.enabled/replicaCount/resources`
- `networkPolicy.privateNetworks`, `publicEgress.except`, `additionalIngress`
- `serviceMonitor.enabled` and the metrics token Secret
- `migration.enabled` and hook behavior

## Migration ordering

`migration.enabled=true` creates a pre-install/pre-upgrade Job. The Kubernetes
Secret must exist before installation. If using External Secrets, bootstrap the
target Secret (or wait for the ExternalSecret to sync) before the first Helm
install; Helm does not wait for ExternalSecret readiness.

## Rollback

```bash
helm rollback marketlens <revision> -n marketlens --wait
```

Rollback does not reverse database migrations. Follow the expand/contract and
restore procedures in `docs/operations-runbook.md`.

## Validation performed

`helm lint` and `helm template` (including optional Ingress, ServiceMonitor,
and ExternalSecret) were run locally. No real cluster apply was performed.
