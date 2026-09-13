"""Add organization administrator invitation and recovery lifecycle."""

from alembic import op
import sqlalchemy as sa


revision = "0017_organization_admin_invitations"
down_revision = "0016_staging_uat_fixtures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The required revision identifier exceeds the production baseline's
    # VARCHAR(32). Widen Alembic's bookkeeping column before Alembic writes the
    # new identifier. It intentionally remains widened on downgrade because
    # Alembic updates the version row only after this function returns.
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=64),
            existing_nullable=False,
        )
    op.add_column(
        "organization_staff",
        sa.Column("credential_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "organization_staff",
        sa.Column("credentials_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_organization_staff_credential_version",
        "organization_staff",
        "credential_version >= 1",
    )

    op.create_table(
        "organization_admin_invitations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("created_by_principal_type", sa.String(length=32), nullable=False),
        sa.Column("created_by_principal_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('bootstrap', 'invite', 'recovery')",
            name="ck_admin_invitation_purpose",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 10 AND "
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_admin_invitation_attempts",
        ),
        sa.CheckConstraint(
            "NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name="ck_admin_invitation_terminal_state",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("correlation_id"),
    )
    op.create_index(
        "ix_organization_admin_invitations_organization_id",
        "organization_admin_invitations",
        ["organization_id"],
    )
    op.create_index(
        "ix_organization_admin_invitations_token_hash",
        "organization_admin_invitations",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_organization_admin_invitations_expires_at",
        "organization_admin_invitations",
        ["expires_at"],
    )
    op.create_index(
        "ix_admin_invitation_org_active",
        "organization_admin_invitations",
        ["organization_id", "purpose", "used_at", "revoked_at", "expires_at"],
    )
    op.create_index(
        "ix_admin_invitation_org_email",
        "organization_admin_invitations",
        ["organization_id", "email", "purpose"],
    )

    op.create_table(
        "organization_admin_invitation_rate_limits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("attempt_count >= 1", name="ck_admin_invitation_rate_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope", "key_hash", "window_started_at", name="uq_admin_invitation_rate_bucket"
        ),
    )
    op.create_index(
        "ix_admin_invitation_rate_expiry",
        "organization_admin_invitation_rate_limits",
        ["expires_at", "id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE organization_admin_invitations, organization_staff "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    active_exists = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM organization_admin_invitations "
            "WHERE used_at IS NULL AND revoked_at IS NULL "
            "AND expires_at > CURRENT_TIMESTAMP)"
        )
    ).scalar_one()
    if active_exists:
        raise RuntimeError(
            "Cannot downgrade 0017 while active administrator invitations remain"
        )
    recovery_state_exists = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM organization_staff "
            "WHERE credential_version > 1 OR credentials_revoked_at IS NOT NULL)"
        )
    ).scalar_one()
    if recovery_state_exists:
        raise RuntimeError(
            "Cannot downgrade 0017 while recovery-derived credential revocation state remains"
        )

    op.drop_index(
        "ix_admin_invitation_rate_expiry",
        table_name="organization_admin_invitation_rate_limits",
    )
    op.drop_table("organization_admin_invitation_rate_limits")
    op.drop_index("ix_admin_invitation_org_email", table_name="organization_admin_invitations")
    op.drop_index("ix_admin_invitation_org_active", table_name="organization_admin_invitations")
    op.drop_index(
        "ix_organization_admin_invitations_expires_at",
        table_name="organization_admin_invitations",
    )
    op.drop_index(
        "ix_organization_admin_invitations_token_hash",
        table_name="organization_admin_invitations",
    )
    op.drop_index(
        "ix_organization_admin_invitations_organization_id",
        table_name="organization_admin_invitations",
    )
    op.drop_table("organization_admin_invitations")
    op.drop_constraint(
        "ck_organization_staff_credential_version",
        "organization_staff",
        type_="check",
    )
    op.drop_column("organization_staff", "credentials_revoked_at")
    op.drop_column("organization_staff", "credential_version")
