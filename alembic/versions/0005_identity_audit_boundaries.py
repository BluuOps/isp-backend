"""Add identity and audit boundaries before customer portal work."""

from alembic import op
import sqlalchemy as sa


revision = "0005_identity_audit_boundaries"
down_revision = "0004_payments_audit_logs"
branch_labels = None
depends_on = None


CUSTOMER_PORTAL_STATUSES = ("invited", "active", "suspended", "locked", "disabled")
PRINCIPAL_TYPES = ("platform_admin", "organization_staff", "customer", "system")


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("actor_type", sa.String(length=50), nullable=True))
    op.add_column("audit_logs", sa.Column("actor_id", sa.String(length=255), nullable=True))
    op.add_column("audit_logs", sa.Column("actor_label", sa.String(length=255), nullable=True))

    op.execute(sa.text("""
        UPDATE audit_logs
        SET
            actor_type = CASE
                WHEN actor = 'platform-admin' THEN 'platform_admin'
                WHEN actor IN ('internal-admin', 'organization-admin') THEN 'organization_staff'
                WHEN actor IS NULL OR actor = '' THEN 'system'
                ELSE 'system'
            END,
            actor_id = CASE
                WHEN actor IN ('platform-admin', 'internal-admin', 'organization-admin') THEN actor
                ELSE NULL
            END,
            actor_label = COALESCE(NULLIF(actor, ''), 'system')
        WHERE actor_type IS NULL
    """))
    op.alter_column("audit_logs", "actor_type", existing_type=sa.String(length=50), nullable=False)
    op.create_check_constraint(
        "ck_audit_logs_actor_type",
        "audit_logs",
        f"actor_type IN {PRINCIPAL_TYPES}",
    )
    op.create_index("ix_audit_logs_organization_id", "audit_logs", ["organization_id"])
    op.create_index("ix_audit_logs_actor_type", "audit_logs", ["actor_type"])
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    op.create_unique_constraint("uq_customers_org_id_id", "customers", ["organization_id", "id"])
    op.create_table(
        "customer_portal_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("customer_id", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=150), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("password_hash", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="invited"),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "customer_id", name="uq_customer_portal_org_customer"),
        sa.UniqueConstraint("organization_id", "email", name="uq_customer_portal_org_email"),
        sa.UniqueConstraint("organization_id", "phone", name="uq_customer_portal_org_phone"),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_customer_portal_same_org_customer",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"status IN {CUSTOMER_PORTAL_STATUSES}", name="ck_customer_portal_status"),
    )
    op.create_index("ix_customer_portal_accounts_organization_id", "customer_portal_accounts", ["organization_id"])
    op.create_index("ix_customer_portal_accounts_customer_id", "customer_portal_accounts", ["customer_id"])
    op.create_index("ix_customer_portal_accounts_email", "customer_portal_accounts", ["email"])
    op.create_index("ix_customer_portal_accounts_phone", "customer_portal_accounts", ["phone"])
    op.create_index("ix_customer_portal_accounts_status", "customer_portal_accounts", ["status"])

    op.add_column("payment_transactions", sa.Column("created_by_staff_id", sa.Integer(), nullable=True))
    op.add_column("payment_transactions", sa.Column("created_by_customer_id", sa.String(length=100), nullable=True))
    op.add_column("payment_transactions", sa.Column("created_by_principal_type", sa.String(length=50), nullable=True))
    op.add_column("payment_transactions", sa.Column("recorded_by_label", sa.String(length=255), nullable=True))
    op.create_foreign_key(
        "fk_payment_created_by_staff_id",
        "payment_transactions",
        "organization_staff",
        ["created_by_staff_id"],
        ["id"],
        onupdate="CASCADE",
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_payment_created_by_customer_id",
        "payment_transactions",
        "customers",
        ["created_by_customer_id"],
        ["id"],
        onupdate="CASCADE",
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_payment_created_by_one_identity",
        "payment_transactions",
        "(created_by_staff_id IS NULL OR created_by_customer_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_payment_created_by_principal_type",
        "payment_transactions",
        "created_by_principal_type IS NULL OR created_by_principal_type IN ('organization_staff', 'customer', 'system')",
    )
    op.create_index("ix_payment_transactions_created_by_staff_id", "payment_transactions", ["created_by_staff_id"])
    op.create_index("ix_payment_transactions_created_by_customer_id", "payment_transactions", ["created_by_customer_id"])
    op.create_index("ix_payment_transactions_created_by_principal_type", "payment_transactions", ["created_by_principal_type"])


def downgrade() -> None:
    op.drop_index("ix_payment_transactions_created_by_principal_type", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_created_by_customer_id", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_created_by_staff_id", table_name="payment_transactions")
    op.drop_constraint("ck_payment_created_by_principal_type", "payment_transactions", type_="check")
    op.drop_constraint("ck_payment_created_by_one_identity", "payment_transactions", type_="check")
    op.drop_constraint("fk_payment_created_by_customer_id", "payment_transactions", type_="foreignkey")
    op.drop_constraint("fk_payment_created_by_staff_id", "payment_transactions", type_="foreignkey")
    op.drop_column("payment_transactions", "recorded_by_label")
    op.drop_column("payment_transactions", "created_by_principal_type")
    op.drop_column("payment_transactions", "created_by_customer_id")
    op.drop_column("payment_transactions", "created_by_staff_id")

    op.drop_index("ix_customer_portal_accounts_status", table_name="customer_portal_accounts")
    op.drop_index("ix_customer_portal_accounts_phone", table_name="customer_portal_accounts")
    op.drop_index("ix_customer_portal_accounts_email", table_name="customer_portal_accounts")
    op.drop_index("ix_customer_portal_accounts_customer_id", table_name="customer_portal_accounts")
    op.drop_index("ix_customer_portal_accounts_organization_id", table_name="customer_portal_accounts")
    op.drop_table("customer_portal_accounts")
    op.drop_constraint("uq_customers_org_id_id", "customers", type_="unique")

    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_organization_id", table_name="audit_logs")
    op.drop_constraint("ck_audit_logs_actor_type", "audit_logs", type_="check")
    op.drop_column("audit_logs", "actor_label")
    op.drop_column("audit_logs", "actor_id")
    op.drop_column("audit_logs", "actor_type")
