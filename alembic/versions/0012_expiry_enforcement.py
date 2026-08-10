"""Add tenant-scoped subscription expiry enforcement outbox."""

from alembic import op
import sqlalchemy as sa


revision = "0012_expiry_enforcement"
down_revision = "0011_plan_change_activation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_users_id_organization", "users", ["id", "organization_id"])
    op.create_unique_constraint(
        "uq_network_access_servers_id_organization",
        "network_access_servers",
        ["id", "organization_id"],
    )
    op.create_index(
        "ix_users_expiry_scan",
        "users",
        ["organization_id", "expiration_date", "id"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "expiry_scan_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("correlation_id", sa.String(length=120), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("component", sa.String(length=100), nullable=False, server_default="expiry-scan-worker"),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("evaluated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("newly_expired_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_session_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("jobs_queued_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','completed','completed_with_errors','failed')",
            name="ck_expiry_scan_run_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("correlation_id", name="uq_expiry_scan_run_correlation"),
    )
    op.create_index("ix_expiry_scan_runs_organization_id", "expiry_scan_runs", ["organization_id"])
    op.create_index("ix_expiry_scan_runs_correlation_id", "expiry_scan_runs", ["correlation_id"])
    op.create_index("ix_expiry_scan_runs_completed_at", "expiry_scan_runs", ["completed_at"])

    op.create_table(
        "expiry_disconnect_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_radacct_id", sa.BigInteger(), nullable=False),
        sa.Column("session_identity", sa.String(length=255), nullable=False),
        sa.Column("nas_id", sa.Integer(), nullable=True),
        sa.Column("reason_code", sa.String(length=50), nullable=False),
        sa.Column("requested_expiration_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("correlation_id", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','processing','succeeded','retryable_failure','terminal_failure','cancelled','stale')",
            name="ck_expiry_disconnect_job_status",
        ),
        sa.CheckConstraint("attempt_count >= 0 AND max_attempts BETWEEN 1 AND 10", name="ck_expiry_disconnect_attempts"),
        sa.ForeignKeyConstraint(
            ["user_id", "organization_id"],
            ["users.id", "users.organization_id"],
            name="fk_expiry_disconnect_job_user_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["nas_id", "organization_id"],
            ["network_access_servers.id", "network_access_servers.organization_id"],
            name="fk_expiry_disconnect_job_nas_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("correlation_id", name="uq_expiry_disconnect_job_correlation"),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "session_identity",
            "requested_expiration_at",
            name="uq_expiry_disconnect_event_session",
        ),
    )
    op.create_index("ix_expiry_disconnect_jobs_organization_id", "expiry_disconnect_jobs", ["organization_id"])
    op.create_index("ix_expiry_disconnect_jobs_customer_id", "expiry_disconnect_jobs", ["customer_id"])
    op.create_index("ix_expiry_disconnect_jobs_user_id", "expiry_disconnect_jobs", ["user_id"])
    op.create_index("ix_expiry_disconnect_jobs_status", "expiry_disconnect_jobs", ["status"])
    op.create_index("ix_expiry_disconnect_jobs_correlation_id", "expiry_disconnect_jobs", ["correlation_id"])
    op.create_index(
        "ix_expiry_disconnect_jobs_due",
        "expiry_disconnect_jobs",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status IN ('pending','retryable_failure')"),
    )


def downgrade() -> None:
    op.drop_index("ix_expiry_disconnect_jobs_due", table_name="expiry_disconnect_jobs")
    op.drop_index("ix_expiry_disconnect_jobs_correlation_id", table_name="expiry_disconnect_jobs")
    op.drop_index("ix_expiry_disconnect_jobs_status", table_name="expiry_disconnect_jobs")
    op.drop_index("ix_expiry_disconnect_jobs_user_id", table_name="expiry_disconnect_jobs")
    op.drop_index("ix_expiry_disconnect_jobs_customer_id", table_name="expiry_disconnect_jobs")
    op.drop_index("ix_expiry_disconnect_jobs_organization_id", table_name="expiry_disconnect_jobs")
    op.drop_table("expiry_disconnect_jobs")
    op.drop_index("ix_expiry_scan_runs_completed_at", table_name="expiry_scan_runs")
    op.drop_index("ix_expiry_scan_runs_correlation_id", table_name="expiry_scan_runs")
    op.drop_index("ix_expiry_scan_runs_organization_id", table_name="expiry_scan_runs")
    op.drop_table("expiry_scan_runs")
    op.drop_index("ix_users_expiry_scan", table_name="users")
    op.drop_constraint("uq_network_access_servers_id_organization", "network_access_servers", type_="unique")
    op.drop_constraint("uq_users_id_organization", "users", type_="unique")
