"""Add tenant-scoped customer plan purchasing metadata."""

from alembic import op
import sqlalchemy as sa


revision = "0010_customer_plan_payments"
down_revision = "0009_network_access_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The production baseline was originally created from SQLAlchemy metadata
    # with both ``unique=True`` and ``index=True`` on ``name``. PostgreSQL
    # therefore has a unique index, not a ``service_plans_name_key``
    # constraint.
    op.drop_index("ix_service_plans_name", table_name="service_plans")
    op.create_index("ix_service_plans_name", "service_plans", ["name"], unique=False)
    op.create_unique_constraint("uq_service_plan_org_name", "service_plans", ["organization_id", "name"])
    op.add_column("service_plans", sa.Column("price_minor", sa.BigInteger(), nullable=True))
    op.add_column("service_plans", sa.Column("currency", sa.String(length=3), nullable=False, server_default="NGN"))
    op.add_column("service_plans", sa.Column("duration_days", sa.Integer(), nullable=False, server_default="30"))
    op.add_column("service_plans", sa.Column("billing_interval", sa.String(length=30), nullable=False, server_default="monthly"))
    op.add_column("service_plans", sa.Column("customer_visible", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index(
        "ix_service_plans_customer_catalog",
        "service_plans",
        ["organization_id", "status", "customer_visible"],
    )

    op.add_column("payment_transactions", sa.Column("selected_plan_id", sa.Integer(), nullable=True))
    op.add_column("payment_transactions", sa.Column("billing_periods", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("payment_transactions", sa.Column("quote_reference", sa.String(length=120), nullable=True))
    op.add_column(
        "payment_transactions",
        sa.Column("fulfillment_status", sa.String(length=40), nullable=False, server_default="not_applicable"),
    )
    op.add_column("payment_transactions", sa.Column("previous_plan_name", sa.String(length=100), nullable=True))
    op.add_column("payment_transactions", sa.Column("resulting_plan_name", sa.String(length=100), nullable=True))
    op.create_foreign_key(
        "fk_payment_selected_plan",
        "payment_transactions",
        "service_plans",
        ["selected_plan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint("uq_payment_quote_reference", "payment_transactions", ["quote_reference"])
    op.create_index("ix_payment_transactions_selected_plan_id", "payment_transactions", ["selected_plan_id"])
    op.create_index("ix_payment_transactions_fulfillment_status", "payment_transactions", ["fulfillment_status"])
    op.create_index(
        "uq_payment_gateway_reference",
        "payment_transactions",
        ["gateway", "gateway_reference"],
        unique=True,
        postgresql_where=sa.text("gateway_reference IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_payment_gateway_reference", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_fulfillment_status", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_selected_plan_id", table_name="payment_transactions")
    op.drop_constraint("uq_payment_quote_reference", "payment_transactions", type_="unique")
    op.drop_constraint("fk_payment_selected_plan", "payment_transactions", type_="foreignkey")
    for column in (
        "resulting_plan_name",
        "previous_plan_name",
        "fulfillment_status",
        "quote_reference",
        "billing_periods",
        "selected_plan_id",
    ):
        op.drop_column("payment_transactions", column)

    op.drop_index("ix_service_plans_customer_catalog", table_name="service_plans")
    for column in ("customer_visible", "billing_interval", "duration_days", "currency", "price_minor"):
        op.drop_column("service_plans", column)
    op.drop_constraint("uq_service_plan_org_name", "service_plans", type_="unique")
    op.drop_index("ix_service_plans_name", table_name="service_plans")
    op.create_index("ix_service_plans_name", "service_plans", ["name"], unique=True)
