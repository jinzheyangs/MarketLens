# ADR 0017: PostgreSQL-backed monitoring, encrypted notifications, and tenant quotas

- Status: Accepted
- Date: 2026-11-23

## Context

Stage 8 adds recurring market monitoring, change detection, alert delivery, project
collaboration, and resource governance. The existing platform already has a hardened
collector (SSRF-safe fetching, DNS pinning, robots, rate limits), report provenance,
transactional outbox, and a PostgreSQL queue fallback. We need continuous execution
without introducing a second mandatory orchestration dependency for every deployment,
and notifications must not become a new SSRF/data-leak channel.

## Decision

1. **PostgreSQL queue scheduler.** `monitor_runs` and `notification_deliveries` use
   lease-based claims (`FOR UPDATE SKIP LOCKED`), bounded retries, and expiry-based
   recovery. A dedicated worker (`app.monitoring.worker`) performs scheduling,
   collection orchestration, change detection, and delivery. Temporal remains used for
   research lifecycle workflows only; monitoring does not require it.
2. **Reuse the collector for egress and snapshots.** Monitoring only selects URLs; all
   retrieval, robots, DNS pinning, domain rate limiting, and snapshot storage go through
   the existing collection task pipeline. Search results pass the same URL policy as
   explicitly submitted URLs.
3. **Hash-based change detection.** A per-subscription `monitor_seen_sources` row stores
   the last snapshot ID and content SHA-256. New source IDs produce `new_source`;
   differing hashes produce `content_changed`. We do not diff rendered HTML in this
   stage because parsing arbitrary pages is a larger correctness/security surface.
4. **Encrypted channel configuration.** Tenant-supplied webhook URLs and signing
   secrets are encrypted with Fernet using a key from environment/secret manager.
   API responses expose metadata only. Decryption happens immediately before delivery.
5. **Safe notification egress.** Webhooks use HTTPS by default, public-address
   validation, pinned connections, no redirects, bounded time/size, and optional
   domain allowlisting. HMAC signing and delivery idempotency headers are supported.
6. **Tenant quotas via advisory locks.** `tenant_quotas` stores per-tenant limits.
   Every enforcement path takes a transaction-scoped advisory lock keyed by
   tenant+resource before counting and inserting, preventing concurrent overrun.
7. **Collaboration objects are additive.** Comments and approval nodes do not mutate
   report state. Formal report approval remains the checksum-verified admin action.

## Consequences

- Operators get monitoring without Kafka/Temporal, but the scheduler polls the database
  and is not intended for very large subscription counts; sharding can be added without
  changing the API.
- Notification delivery is at-least-once. Receivers must deduplicate by delivery ID.
- Key rotation requires a re-encryption migration or multi-key support; this stage
  documents the limitation rather than shipping an unsafe automatic rotation path.
- Quotas are soft with respect to already-running work and can transiently exceed by a
  small amount at concurrency boundaries; the lock closes the common read-then-write
  race, not long-running jobs.
- Approval nodes and report approval are intentionally separate. Integrating them
  later must preserve checksum verification and admin-only final approval.
