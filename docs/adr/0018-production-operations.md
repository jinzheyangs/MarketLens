# ADR 0018: Production operations, SLOs, and data lifecycle boundaries

- Status: Accepted
- Date: 2026-11-24

## Context

Stage 9 must make the platform deployable and operable without overstating what
has been validated. The repository cannot provision a real cluster, cloud
account, Kafka/MSK, Temporal, or Qdrant installation, and it must not claim
compliance or availability guarantees on behalf of the deploying organization.

## Decision

1. **Helm is the application deployment unit.** API and all workers share one
   image; workers differ only by command. Migrations run as a pre-install/
   pre-upgrade Helm hook using `alembic upgrade head`. Production rollback uses
   `helm rollback`, with the explicit warning that schema rollback is separate
   and must follow expand/contract.
2. **Terraform owns stateful cloud dependencies.** The sample provisions
   encrypted Multi-AZ RDS, a versioned/private S3 bucket, KMS, and Secrets
   Manager. State must use an encrypted remote backend; no credentials or
   passwords are committed.
3. **Secrets are external references.** The chart consumes an existing Secret or
   an ExternalSecret. `values.yaml` contains no secret values. Production
   validation refuses metrics without a bearer token and monitoring without a
   Fernet key.
4. **NetworkPolicy defaults to deny.** Ingress is limited to the ingress
   namespace; egress allows DNS, private dependency ports, and public 80/443 with
   link-local/private ranges excluded. Deployers must tighten CIDRs to their
   environment.
5. **Prometheus is the SLO source of truth.** `/metrics` exposes low-cardinality
   HTTP metrics behind a bearer token. Recording/alert rules and a Grafana
   dashboard are versioned. Worker-queue metrics are not exposed; deployment
   status and logs cover worker health until a worker metrics endpoint is added.
6. **Retention is bounded and opt-in.** The retention job only deletes
   operational rows (processed events, resolved consumer failures, published
   outbox rows) after an explicit cutoff, with dry-run by default and
   `RETENTION_DESTRUCTIVE_ENABLED=true` for execution. Business records require
   a separate approved tenant data-lifecycle process.
7. **RPO/RTO are deployment-owned targets.** Terraform and runbooks record
   declared values, but measured recovery time and data loss windows come from
   restore/failover drills performed by the operator.
8. **Supply chain artifacts are generated in CI.** Images are scanned, SBOMs are
   produced, and releases are signed. These controls are samples until the
   organization wires them into its admission and release policies.

## Consequences

- Operators must supply cluster, ingress, DNS, TLS, VPC, Kafka/MSK, Temporal,
  Qdrant, and workload identity. The chart and Terraform do not provide a
  turnkey platform.
- Helm/terraform validation is syntactic and schema-level; no cloud apply or
  restore drill was performed here.
- Metrics and alerts can page on API symptoms, but queue backlog alerting
  requires a follow-up worker metrics implementation.
- Business-data retention, legal holds, and customer deletion remain manual,
  auditable processes rather than automatic jobs.
