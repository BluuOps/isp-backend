from alembic import op
import sqlalchemy as sa


revision = "0004_payments_audit_logs"
down_revision = "0003_commercial_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("customer_id", sa.String(length=100), sa.ForeignKey("customers.id", onupdate="CASCADE", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True),
        sa.Column("transaction_reference", sa.String(length=120), nullable=False),
        sa.Column("external_reference", sa.String(length=255), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="NGN"),
        sa.Column("payment_method", sa.String(length=50), nullable=False),
        sa.Column("payment_status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("organization_id", "transaction_reference", name="uq_payment_org_transaction_reference"),
    )
    op.create_index("ix_payment_transactions_id", "payment_transactions", ["id"])
    op.create_index("ix_payment_transactions_organization_id", "payment_transactions", ["organization_id"])
    op.create_index("ix_payment_transactions_customer_id", "payment_transactions", ["customer_id"])
    op.create_index("ix_payment_transactions_user_id", "payment_transactions", ["user_id"])
    op.create_index("ix_payment_transactions_transaction_reference", "payment_transactions", ["transaction_reference"])
    op.create_index("ix_payment_transactions_external_reference", "payment_transactions", ["external_reference"])
    op.create_index("ix_payment_transactions_payment_status", "payment_transactions", ["payment_status"])
    op.create_index("ix_payment_transactions_paid_at", "payment_transactions", ["paid_at"])
    op.create_index("ix_payment_transactions_created_at", "payment_transactions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_payment_transactions_created_at", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_paid_at", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_payment_status", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_external_reference", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_transaction_reference", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_user_id", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_customer_id", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_organization_id", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_id", table_name="payment_transactions")
    op.drop_table("payment_transactions")
