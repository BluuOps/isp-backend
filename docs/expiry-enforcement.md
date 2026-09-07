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
`Expiration` control, and adds `Auth-Type := Reject` whenever policy denies access. Every reject
created by RadiusFiber is linked to its exact `radcheck` row in `radius_reject_ownerships`, including
the tenant, service, and denial reason. Renewal removes only that exact owned row when administrative
state and plan remain eligible. Manual session disconnects use the distinct reason
`MANUAL_DISCONNECT`; manual and policy reasons may coexist without being overwritten. Reconnect
removes only its owned manual/suspension reasons. If an expiry or unknown reject remains, the API
returns `409` and does not claim that authentication was restored. A pre-existing or manually
created reject is never claimed or removed. Suspended and terminated services are never resumed
by renewal.

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

Dry scans start a PostgreSQL read-only transaction, create no scan/audit/job/authorization rows,
take no worker lock, and always roll back. Dry scan:

```bash
python -m app.scripts.expiry_worker scan --dry-run
```

Tenant dry scan:

```bash
python -m app.scripts.expiry_worker scan --dry-run --organization-id 1
```

Exact tenant-bound canary dry scan (all three selectors are mandatory together):

```bash
python -m app.scripts.expiry_worker scan --dry-run \
  --organization-id 1 --user-id 11 --username TEST_ONU
```

The ID and username must both match within the selected organization. A mismatch returns zero
services rather than broadening the selection. Exact-target output always distinguishes `matched`,
`evaluated`, `would_change`, `changed`, `disconnected`, and `errors`. A dry run reports prospective
authorization mutations in `would_change` and always reports `changed=0`; a live scan reports
authorization mutations performed by the scan in `changed`. Exit code `0` requires exactly one matched and
evaluated service with no errors. Exit code `2` means an invalid selector, `3` means cardinality
failure, `4` means an item failed, and `5` means the scan/database operation failed. A safely
pre-enforced target may report `changed=0` and still succeed. Scanning queues rather than executes
disconnects, so `disconnected` is always `0` in scan output.

Dry scans report `acquired_lock=false` and `lock_skipped=true`; live scans retain advisory-lock
enforcement.

## Legacy reject reconciliation

An unowned effective reject is fail-closed and blocks normal lifecycle reconciliation. Produce an
exact-row, read-only report first:

```bash
python -m app.scripts.reject_reconciliation \
  --organization-id ORGANIZATION_ID \
  --user-id USER_ID \
  --username USERNAME \
  --radcheck-id RADCHECK_ID
```

Only after independently verifying auditable evidence, one identified row can be adopted or
removed by adding `--action adopt` (or `remove`), `--apply`, an exactly matching
`--confirm-radcheck-id`, `--evidence-reference`, and `--operator-id`. The command has no bulk mode;
every applied action writes a sanitized audit event. Unknown or administrator-created rejects must
not be adopted or removed.

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

Production freshness SQL uses PostgreSQL `CURRENT_TIMESTAMP`; deterministic tests explicitly inject
an aware reference time. Customer Portal object-denial events are emitted as bounded JSON through
the `radiusfiber.security` application logger. The systemd service pipeline sends stderr/stdout to
journald. Durable retention and alerting for these events must be configured and verified before
the journal retention window is treated as long-term security evidence.

`PERF-RADACCT-001`: benchmark the fresh-session predicate with representative `radacct` volume and
query plans, then evaluate a PostgreSQL partial expression index for open rows covering
`COALESCE(acctupdatetime, acctstarttime)`. Do not add the index without measured justification.

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
3. Apply the reviewed migration chain through `0015_expiry_reject_ownership` with the migration owner.
4. Validate health, readiness, privileges, dry scan, mock lifecycle, and tenant isolation.
5. Install staging-only units only after validating their rendered paths and environment.
6. Keep production timer disabled until a separate `TEST_ONU` canary is approved.

Application rollback may deploy the previous code only after stopping the worker. Because older code
cannot honor reject ownership, schema/code rollback requires a separately reviewed fail-closed plan.
Downgrading `0015` deliberately retains all RADIUS reject rows and removes only ownership metadata;
never downgrade while any worker process is active.

Alert on: no successful scan for two schedule intervals, repeated lock contention, terminal jobs,
retry backlog growth, schema/readiness failure, or a processing job exceeding its timeout. Manual
recovery must never expose secrets or mark accounting sessions stopped without NAS evidence.

## Production canary plan

A separate gate must select only `TEST_ONU`, capture its authorization and accounting baseline,
enable one allowlisted NAS target, validate rejection after expiration, send at most one controlled
disconnect, observe a real stop/accounting update, renew without resuming suspension, and restore
the prior configuration on any unexpected result. No other subscriber may be included.
