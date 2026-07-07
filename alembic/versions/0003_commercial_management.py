"""Add commercial organization onboarding objects."""

from alembic import op
import sqlalchemy as sa

revision = "0003_commercial_management"
down_revision = "0002_saas_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_staff",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(500), nullable=False),
        sa.Column("role", sa.String(100), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("is_temporary_password", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "email", name="uq_staff_org_email"),
    )
    op.create_index("ix_organization_staff_organization_id", "organization_staff", ["organization_id"])
    op.create_table(
        "organization_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_org_roles_org_name"),
    )
    op.create_index("ix_organization_roles_organization_id", "organization_roles", ["organization_id"])
    op.create_table(
        "zones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_zones_org_name"),
    )
    op.create_index("ix_zones_organization_id", "zones", ["organization_id"])
    op.create_table(
        "organization_billing_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, unique=True),
        sa.Column("billing_email", sa.String(255)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "notification_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, unique=True),
        sa.Column("email_enabled", sa.Boolean(), nullable=False),
        sa.Column("sms_enabled", sa.Boolean(), nullable=False),
        sa.Column("expiration_alerts", sa.Boolean(), nullable=False),
        sa.Column("payment_alerts", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(sa.text("""
        INSERT INTO organization_billing_profiles
            (organization_id, billing_email, currency, status)
        SELECT id, company_email, currency, 'active'
        FROM organizations o
        WHERE NOT EXISTS (
            SELECT 1 FROM organization_billing_profiles b
            WHERE b.organization_id = o.id
        )
    """))
    op.execute(sa.text("""
        INSERT INTO notification_settings
            (organization_id, email_enabled, sms_enabled, expiration_alerts, payment_alerts)
        SELECT id, false, false, true, true
        FROM organizations o
        WHERE NOT EXISTS (
            SELECT 1 FROM notification_settings n
            WHERE n.organization_id = o.id
        )
    """))
    op.execute(sa.text("""
        INSERT INTO zones (organization_id, name, status)
        SELECT id, 'Default', 'active'
        FROM organizations o
        WHERE NOT EXISTS (
            SELECT 1 FROM zones z WHERE z.organization_id = o.id AND z.name = 'Default'
        )
    """))
    op.execute(sa.text("""
        INSERT INTO subscriptions
            (organization_id, plan, status, starts_at, expires_at, next_billing_date, auto_renew)
        SELECT id, COALESCE(subscription_plan, 'Legacy'),
               COALESCE(subscription_status, 'active'),
               COALESCE(subscription_started_at, created_at),
               subscription_expires_at, subscription_expires_at, false
        FROM organizations o
        WHERE NOT EXISTS (
            SELECT 1 FROM subscriptions s WHERE s.organization_id = o.id
        )
    """))
    for role_name in (
        "Organization Admin", "NOC", "Billing", "Support",
        "Field Engineer", "Read Only", "Customer",
    ):
        op.execute(sa.text("""
            INSERT INTO organization_roles (organization_id, name)
            SELECT id, :role_name FROM organizations o
            WHERE NOT EXISTS (
                SELECT 1 FROM organization_roles r
                WHERE r.organization_id = o.id AND r.name = :role_name
            )
        """).bindparams(role_name=role_name))
    for flag_key in (
        "customer_portal", "payment_gateway", "organization_billing",
        "notifications", "gis", "olt_management", "inventory",
        "ai_assistant", "enforce_limits",
    ):
        op.execute(sa.text("""
            INSERT INTO feature_flags (organization_id, key, enabled)
            SELECT id, :flag_key, false FROM organizations o
            WHERE NOT EXISTS (
                SELECT 1 FROM feature_flags f
                WHERE f.organization_id = o.id AND f.key = :flag_key
            )
        """).bindparams(flag_key=flag_key))


def downgrade() -> None:
    op.drop_table("notification_settings")
    op.drop_table("organization_billing_profiles")
    op.drop_index("ix_zones_organization_id", table_name="zones")
    op.drop_table("zones")
    op.drop_index("ix_organization_staff_organization_id", table_name="organization_staff")
    op.drop_index("ix_organization_roles_organization_id", table_name="organization_roles")
    op.drop_table("organization_roles")
    op.drop_table("organization_staff")
