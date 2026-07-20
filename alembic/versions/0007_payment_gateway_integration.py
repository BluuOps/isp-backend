"""Add Paystack payment gateway integration fields."""

from alembic import op
import sqlalchemy as sa


revision = "0007_payment_gateway_integration"
down_revision = "0006_customer_portal_backend_mvp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_transactions", sa.Column("payment_purpose", sa.String(length=50), nullable=False, server_default="manual_payment"))
    op.add_column("payment_transactions", sa.Column("gateway", sa.String(length=50), nullable=True))
    op.add_column("payment_transactions", sa.Column("gateway_reference", sa.String(length=255), nullable=True))
    op.add_column("payment_transactions", sa.Column("authorization_url", sa.Text(), nullable=True))
    op.add_column("payment_transactions", sa.Column("access_code", sa.String(length=255), nullable=True))
    op.add_column("payment_transactions", sa.Column("idempotency_key", sa.String(length=120), nullable=True))
    op.add_column("payment_transactions", sa.Column("initiated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("raw_gateway_status", sa.String(length=100), nullable=True))
    op.add_column("payment_transactions", sa.Column("gateway_metadata", sa.JSON(), nullable=True))
    op.add_column("payment_transactions", sa.Column("renewal_processed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("renewal_cycles", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("payment_transactions", sa.Column("expected_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("payment_transactions", sa.Column("expected_currency", sa.String(length=3), nullable=True))
    op.add_column("payment_transactions", sa.Column("old_expiration_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("new_expiration_date", sa.DateTime(timezone=True), nullable=True))

    op.create_index("ix_payment_transactions_gateway", "payment_transactions", ["gateway"])
    op.create_index("ix_payment_transactions_gateway_reference", "payment_transactions", ["gateway_reference"])
    op.create_index("ix_payment_transactions_idempotency_key", "payment_transactions", ["idempotency_key"])
    op.create_index("ix_payment_transactions_initiated_at", "payment_transactions", ["initiated_at"])
    op.create_index("ix_payment_transactions_payment_purpose", "payment_transactions", ["payment_purpose"])
    op.create_index("ix_payment_transactions_renewal_processed_at", "payment_transactions", ["renewal_processed_at"])
    op.create_index("ix_payment_transactions_verified_at", "payment_transactions", ["verified_at"])
    op.create_index("ix_payment_org_gateway_reference", "payment_transactions", ["organization_id", "gateway", "gateway_reference"])
    op.create_index("ix_payment_org_status", "payment_transactions", ["organization_id", "payment_status"])
    op.create_unique_constraint("uq_payment_org_idempotency_key", "payment_transactions", ["organization_id", "idempotency_key"])


def downgrade() -> None:
    op.drop_constraint("uq_payment_org_idempotency_key", "payment_transactions", type_="unique")
    op.drop_index("ix_payment_org_status", table_name="payment_transactions")
    op.drop_index("ix_payment_org_gateway_reference", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_verified_at", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_renewal_processed_at", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_payment_purpose", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_initiated_at", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_idempotency_key", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_gateway_reference", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_gateway", table_name="payment_transactions")

    op.drop_column("payment_transactions", "new_expiration_date")
    op.drop_column("payment_transactions", "old_expiration_date")
    op.drop_column("payment_transactions", "expected_currency")
    op.drop_column("payment_transactions", "expected_amount")
    op.drop_column("payment_transactions", "renewal_cycles")
    op.drop_column("payment_transactions", "renewal_processed_at")
    op.drop_column("payment_transactions", "gateway_metadata")
    op.drop_column("payment_transactions", "raw_gateway_status")
    op.drop_column("payment_transactions", "reversed_at")
    op.drop_column("payment_transactions", "abandoned_at")
    op.drop_column("payment_transactions", "failed_at")
    op.drop_column("payment_transactions", "verified_at")
    op.drop_column("payment_transactions", "initiated_at")
    op.drop_column("payment_transactions", "idempotency_key")
    op.drop_column("payment_transactions", "access_code")
    op.drop_column("payment_transactions", "authorization_url")
    op.drop_column("payment_transactions", "gateway_reference")
    op.drop_column("payment_transactions", "gateway")
    op.drop_column("payment_transactions", "payment_purpose")
