# Organization administrator invitations and recovery

This lifecycle is the only supported way to bootstrap an administrator for an
existing organization, invite another administrator, or recover one exact
administrator. The platform operator creates a short-lived invitation; the
administrator chooses a password in a separate unauthenticated POST request.

## Security boundary

- Creation, listing, and revocation require a recently issued platform-admin
  bearer token carrying `platform.organization_admins.manage`. The legacy
  platform API key is deliberately not accepted for these high-risk operations.
- The organization comes from the URL. Role, permissions, status, password,
  and expiry are server controlled.
- Tokens contain 256 random bits, are returned once, and are stored only as a
  SHA-256 digest. Creation responses are marked `no-store` and `no-cache`.
- Invitations expire after 20 minutes according to database time. Correctness
  never depends on a cleanup worker.
- Creation and acceptance use database-backed rate-limit buckets. Valid token
  failures also consume a bounded invitation attempt.
- Acceptance locks the organization, invitation, and (for recovery) staff row.
  Purpose-specific state changes, password hashing, session invalidation, token
  consumption, and audit events commit atomically.

## Purposes

- `bootstrap`: permitted only for an active or trial organization with no
  active Organization Admin. Acceptance creates exactly one.
- `invite`: permitted only for an active organization and an email not already
  present in that organization. Acceptance creates one Organization Admin.
- `recovery`: permitted only for one exact, active Organization Admin. It never
  creates staff. Acceptance changes the password and increments the staff
  credential version.

Organization access tokens carry the staff credential version. Authentication
compares it with the current database row. Tokens created before migration omit
the claim and are treated as version 1; after a recovery increments the row,
every earlier token becomes invalid immediately. This is the all-session
revocation mechanism; the per-JTI logout denylist remains unchanged.

## Delivery and audit

Until mail delivery exists, the protected creation response is the one-time
delivery channel. Operators must transfer the token through a protected channel
and must never put it in URLs or logs. Status endpoints never return it.

Audit records include the invitation ID, purpose, organization, actor,
correlation ID, reason, and outcome. They never include the plaintext token,
token digest, password, or password hash.

Migration `0017_organization_admin_invitations` refuses downgrade while an
unused, unrevoked, unexpired invitation exists or any staff row retains
recovery-derived credential revocation state (`credential_version > 1` or
`credentials_revoked_at` set). This prevents rollback from silently restoring
pre-recovery sessions or the legacy internal-password bridge. Expired
invitations remain historical evidence and may be removed by a later separately
controlled cleanup.
The migration widens Alembic's revision bookkeeping column from 32 to 64
characters because the approved 0017 revision identifier is longer than the
legacy limit; the widening is intentionally retained after downgrade.
