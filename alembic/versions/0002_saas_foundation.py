"""Add the nullable SaaS foundation without touching FreeRADIUS tables."""

from alembic import op
import sqlalchemy as sa

revision = "0002_saas_foundation"
down_revision = "0001_production_baseline"
branch_labels = None
depends_on = None

FEATURE_KEYS = (
    "customer_portal",
    "payment_gateway",
    "organization_billing",
    "notifications",
    "gis",
    "olt_management",
    "inventory",
    "ai_assistant",
)
ROLE_ROWS = (
    ("platform", "Platform Admin"),
    ("organization", "Organization Admin"),
    ("organization", "NOC"),
    ("organization", "Billing"),
    ("organization", "Support"),
    ("organization", "Field Engineer"),
    ("organization", "Read Only"),
    ("organization", "Customer"),
)


def upgrade() -> None:
    op.create_table(
        "platform",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("support_email", sa.String(255)),
        sa.Column("support_phone", sa.String(50)),
        sa.Column("default_currency", sa.String(3), nullable=False),
        sa.Column("default_timezone", sa.String(100), nullable=False),
        sa.Column("maintenance_mode", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform_id", sa.Integer(), sa.ForeignKey("platform.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("company_email", sa.String(255)),
        sa.Column("company_phone", sa.String(50)),
        sa.Column("website", sa.String(255)),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("logo", sa.String(500)),
        sa.Column("subscription_plan", sa.String(100)),
        sa.Column("subscription_status", sa.String(50), nullable=False),
        sa.Column("subscription_started_at", sa.DateTime(timezone=True)),
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True)),
        sa.Column("trial_expires_at", sa.DateTime(timezone=True)),
        sa.Column("customer_limit", sa.Integer()),
        sa.Column("staff_limit", sa.Integer()),
        sa.Column("nas_limit", sa.Integer()),
        sa.Column("olt_limit", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)

    op.execute(
        sa.text(
            "INSERT INTO platform "
            "(id,name,version,default_currency,default_timezone,maintenance_mode) "
            "VALUES (1,'RadiusFiber','1.0','NGN','Africa/Lagos',false)"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO organizations "
            "(platform_id,name,slug,status,country,timezone,currency,subscription_status) "
            "VALUES (1,'Smart Fiber Limited','smart-fiber','active','NG','Africa/Lagos','NGN','active')"
        )
    )

    for table in ("customers", "users", "service_plans", "billing_accounts"):
        op.add_column(table, sa.Column("organization_id", sa.Integer(), nullable=True))
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
        op.create_foreign_key(
            f"fk_{table}_organization_id",
            table,
            "organizations",
            ["organization_id"],
            ["id"],
        )
        op.execute(
            sa.text(
                f"UPDATE {table} SET organization_id = "
                "(SELECT id FROM organizations WHERE slug = 'smart-fiber') "
                "WHERE organization_id IS NULL"
            )
        )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("plan", sa.String(100), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_billing_date", sa.DateTime(timezone=True)),
        sa.Column("auto_renew", sa.Boolean(), nullable=False),
        sa.Column("payment_provider", sa.String(100)),
        sa.Column("payment_reference", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id")),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(100), nullable=False),
        sa.Column("target_id", sa.String(255)),
        sa.Column("old_value", sa.JSON()),
        sa.Column("new_value", sa.JSON()),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "feature_flags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id")),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("configuration", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "key", name="uq_feature_flags_org_key"),
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(50), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scope", "name", name="uq_roles_scope_name"),
    )
    for key in FEATURE_KEYS:
        op.execute(sa.text("INSERT INTO feature_flags (key,enabled) VALUES (:key,false)").bindparams(key=key))
    for scope, name in ROLE_ROWS:
        op.execute(
            sa.text("INSERT INTO roles (scope,name) VALUES (:scope,:name)").bindparams(
                scope=scope, name=name
            )
        )


def downgrade() -> None:
    for table in ("billing_accounts", "service_plans", "users", "customers"):
        op.drop_constraint(f"fk_{table}_organization_id", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_organization_id", table_name=table)
        op.drop_column(table, "organization_id")
    op.drop_table("roles")
    op.drop_table("feature_flags")
    op.drop_table("audit_logs")
    op.drop_table("subscriptions")
    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")
    op.drop_table("platform")
