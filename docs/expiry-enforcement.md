# Subscription expiry enforcement

## Authority and lifecycle

`users.expiration_date` is the sole subscription-expiration authority. PostgreSQL stores it
as `timestamp with time zone`; application comparisons use aware UTC values. Legacy naive
values are interpreted as UTC during reconciliation. `users.status` remains an independent
administrative lifecycle state: expiration never rewrites it. Online state comes only from
an open `radacct` row (`acctstoptime IS NULL`).

Access is permitted only for an active organization, active customer, active service, active
tenant-owned plan, and an expiration strictly later than the current UTC time. The exact
boundary `expiration_date <= now` is expired.

## Authentication enforcement

RADIUS synchronization preserves the password and rate policy, writes the standard
`Expiration` control, and adds `Auth-Type := Reject` whenever policy denies access. Renewal
removes only the reject control when administrative state and plan remain eligible. Suspended
and terminated services are never resumed by renewal. Writes are deduplicated per username and
attribute.

The current production FreeRADIUS configuration does not enable the `expiration` module.
Consequently, production rollout must include a separately reviewed FreeRADIUS policy canary;
until then, the application-maintained reject control is the compatible enforcement guard.

## Scan and disconnect design

The scanner uses a PostgreSQL advisory transaction lock, bounded indexed queries, and tenant
joins. It synchronizes authentication state and writes one durable disconnect job for each
expiration event and open accounting session. A unique database constraint prevents duplicate
jobs. No network call occurs in the scanner transaction.

The disconnect worker claims one due job with `FOR UPDATE SKIP LOCKED`, validates the tenant,
service expiration, lifecycle state, session identity, and NAS mapping, commits the claim, closes
the database session, and only then calls the adapter. Results are recorded in a new transaction.
A renewal or session replacement makes a pending job stale. Retryable failures use bounded
exponential backoff with jitter; processing jobs are not automatically replayed after a crash,
avoiding an unreviewed duplicate packet.

Adapters default to `disabled`. Real delivery additionally requires `RADIUS_COA_ENABLED=true`,
`RADIUS_DISCONNECT_MODE=real`, an explicit NAS allowlist, a readable secret file, a validated IP
target, and an executable `radclient`. Secrets are never persisted in jobs or logs.

## Operations

Use the service environment and application virtual environment shown by the deployed unit.

Dry scan:

```bash
python -m app.scripts.expiry_worker scan --dry-run
```

Tenant dry scan:

```bash
python -m app.scripts.expiry_worker scan --dry-run --organization-id 1
```

Safe metrics:

```bash
python -m app.scripts.expiry_worker metrics
```

Pending and failed jobs must be inspected with read-only SQL selecting identifiers, status,
attempt counts, next-attempt timestamps, correlation IDs, and sanitized errors only. Never select
password or secret fields. Retry a job by an approved tenant-scoped operational procedure that
changes the existing job back to `retryable_failure`; never insert a duplicate. Cancel a stale job
by setting `cancelled` only after verifying a renewal or replacement session.

An accounting row is considered online only when it has no Stop timestamp and its most recent
Interim-Update (or Start before the first Interim-Update) falls within
`RADIUS_SESSION_FRESHNESS_SECONDS`. The default is 900 seconds. Keep this value longer than the
NAS Interim-Update interval, and validate it before changing the default; old rows without a Stop
packet must not create online totals or disconnect jobs.

Disable active-session enforcement with `EXPIRY_WORKER_ENABLED=false`; authentication state and
normal API operation remain available. A failed disconnect does not mean authentication succeeded:
check the `Auth-Type`/`Expiration` controls separately from job status. For an offline NAS, retain
the retryable job until the bounded attempt policy ends, then investigate the terminal record.

Renewal extends from `max(existing expiration, activation time)`. It does not resume suspended or
terminated services. Verify termination by observing a genuine accounting stop/update from the NAS;
the worker never fabricates a `radacct` stop row.

## Deployment and rollback

1. Back up the target database in custom format and verify its TOC and checksum.
2. Deploy the reviewed application SHA with real disconnect disabled.
3. Apply exactly `alembic upgrade 0012_expiry_enforcement` with the migration owner.
4. Validate health, readiness, privileges, dry scan, mock lifecycle, and tenant isolation.
5. Install staging-only units only after validating their rendered paths and environment.
6. Keep production timer disabled until a separate `TEST_ONU` canary is approved.

Application rollback may deploy the previous code while retaining the `0012` job tables; stop the
worker first. Schema rollback requires all jobs and audit evidence to be preserved externally and a
separate approval, then `alembic downgrade 0011_plan_change_activation`. Do not downgrade while any
worker process is active.

Alert on: no successful scan for two schedule intervals, repeated lock contention, terminal jobs,
retry backlog growth, schema/readiness failure, or a processing job exceeding its timeout. Manual
recovery must never expose secrets or mark accounting sessions stopped without NAS evidence.

## Production canary plan

A separate gate must select only `TEST_ONU`, capture its authorization and accounting baseline,
enable one allowlisted NAS target, validate rejection after expiration, send at most one controlled
disconnect, observe a real stop/accounting update, renew without resuming suspension, and restore
the prior configuration on any unexpected result. No other subscriber may be included.
