"""Add staff-controlled plan-change activation metadata."""

from alembic import op
import sqlalchemy as sa


revision = "0011_plan_change_activation"
down_revision = "0010_customer_plan_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_transactions", sa.Column("purchased_duration_days", sa.Integer(), nullable=True))
    op.add_column("payment_transactions", sa.Column("activation_period_key", sa.String(length=64), nullable=True))
    op.add_column(
        "payment_transactions",
        sa.Column("activation_status", sa.String(length=40), nullable=False, server_default="not_applicable"),
    )
    op.add_column("payment_transactions", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("activated_by_staff_id", sa.Integer(), nullable=True))
    op.add_column("payment_transactions", sa.Column("activation_correlation_id", sa.String(length=120), nullable=True))
    op.add_column("payment_transactions", sa.Column("resolution_status", sa.String(length=40), nullable=True))
    op.add_column("payment_transactions", sa.Column("resolution_reason", sa.Text(), nullable=True))
    op.add_column("payment_transactions", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("resolved_by_staff_id", sa.Integer(), nullable=True))
    op.add_column("payment_transactions", sa.Column("canonical_payment_id", sa.Integer(), nullable=True))

    op.create_foreign_key(
        "fk_payment_activated_by_staff", "payment_transactions", "organization_staff",
        ["activated_by_staff_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_payment_resolved_by_staff", "payment_transactions", "organization_staff",
        ["resolved_by_staff_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_payment_canonical_payment", "payment_transactions", "payment_transactions",
        ["canonical_payment_id"], ["id"], ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_payment_activation_status",
        "payment_transactions",
        "activation_status IN ('not_applicable','pending_activation','activated','blocked_duplicate','cancelled','resolved_without_activation')",
    )
    op.create_check_constraint(
        "ck_payment_activation_authority",
        "payment_transactions",
        "activation_status = 'not_applicable' OR (activation_period_key IS NOT NULL AND purchased_duration_days > 0)",
    )
    op.create_unique_constraint(
        "uq_payment_activation_correlation_id", "payment_transactions", ["activation_correlation_id"]
    )

    # Snapshot the authoritative plan duration and derive a stable entitlement-period
    # identity for already-verified plan changes. Future periods differ because their
    # source plan and/or entitlement boundary differs after activation.
    op.execute("""
        UPDATE payment_transactions AS payment
        SET purchased_duration_days = plan.duration_days,
            activation_period_key = md5(concat_ws('|',
                payment.organization_id::text,
                payment.customer_id,
                payment.user_id::text,
                coalesce(payment.previous_plan_name, ''),
                payment.selected_plan_id::text,
                coalesce(payment.old_expiration_date::text, 'no-expiration'),
                payment.billing_periods::text
            )),
            activation_status = 'pending_activation',
            resolution_status = 'unresolved'
        FROM service_plans AS plan
        WHERE payment.selected_plan_id = plan.id
          AND payment.payment_status IN ('successful', 'paid')
          AND payment.fulfillment_status = 'pending_activation'
    """)

    op.create_index("ix_payment_activation_period_key", "payment_transactions", ["activation_period_key"])
    op.create_index("ix_payment_activation_status", "payment_transactions", ["activation_status"])
    op.create_index("ix_payment_resolution_status", "payment_transactions", ["resolution_status"])
    op.create_index(
        "uq_payment_activated_logical_period",
        "payment_transactions",
        ["organization_id", "activation_period_key"],
        unique=True,
        postgresql_where=sa.text("activation_status = 'activated'"),
    )
    op.create_index(
        "uq_payment_canonical_logical_period",
        "payment_transactions",
        ["organization_id", "activation_period_key"],
        unique=True,
        postgresql_where=sa.text("resolution_status = 'canonical'"),
    )


def downgrade() -> None:
    for name in (
        "uq_payment_canonical_logical_period",
        "uq_payment_activated_logical_period",
        "ix_payment_resolution_status",
        "ix_payment_activation_status",
        "ix_payment_activation_period_key",
    ):
        op.drop_index(name, table_name="payment_transactions")
    op.drop_constraint("uq_payment_activation_correlation_id", "payment_transactions", type_="unique")
    op.drop_constraint("ck_payment_activation_authority", "payment_transactions", type_="check")
    op.drop_constraint("ck_payment_activation_status", "payment_transactions", type_="check")
    op.drop_constraint("fk_payment_canonical_payment", "payment_transactions", type_="foreignkey")
    op.drop_constraint("fk_payment_resolved_by_staff", "payment_transactions", type_="foreignkey")
    op.drop_constraint("fk_payment_activated_by_staff", "payment_transactions", type_="foreignkey")
    for column in (
        "canonical_payment_id",
        "resolved_by_staff_id",
        "resolved_at",
        "resolution_reason",
        "resolution_status",
        "activation_correlation_id",
        "activated_by_staff_id",
        "activated_at",
        "activation_status",
        "activation_period_key",
        "purchased_duration_days",
    ):
        op.drop_column("payment_transactions", column)
