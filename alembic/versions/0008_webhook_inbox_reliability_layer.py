"""Add persistent payment webhook inbox for reliability layer."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_webhook_inbox"
down_revision = "0007_payment_gateway_integration"
branch_labels = None
depends_on = None


WEBHOOK_STATUSES = (
    "received",
    "queued",
    "processing",
    "processed",
    "ignored",
    "retry_pending",
    "failed",
    "dead_lettered",
)


def upgrade() -> None:
    op.create_table(
        "payment_webhook_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=True),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("transaction_reference", sa.String(length=120), nullable=True),
        sa.Column("gateway_reference", sa.String(length=255), nullable=True),
        sa.Column("signature_valid", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("processing_status", sa.String(length=30), nullable=False, server_default="received"),
        sa.Column("processing_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "processing_status IN ('{}')".format("','".join(WEBHOOK_STATUSES)),
            name="ck_payment_webhook_events_processing_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], onupdate="CASCADE", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["payment_id"], ["payment_transactions.id"], onupdate="CASCADE", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "deduplication_key", name="uq_payment_webhook_provider_deduplication_key"),
    )
    op.create_index("ix_payment_webhook_provider", "payment_webhook_events", ["provider"])
    op.create_index("ix_payment_webhook_provider_event_id", "payment_webhook_events", ["provider_event_id"])
    op.create_index("ix_payment_webhook_transaction_reference", "payment_webhook_events", ["transaction_reference"])
    op.create_index("ix_payment_webhook_gateway_reference", "payment_webhook_events", ["gateway_reference"])
    op.create_index("ix_payment_webhook_processing_status", "payment_webhook_events", ["processing_status"])
    op.create_index("ix_payment_webhook_next_retry_at", "payment_webhook_events", ["next_retry_at"])
    op.create_index("ix_payment_webhook_created_at", "payment_webhook_events", ["created_at"])
    op.create_index("ix_payment_webhook_organization_id", "payment_webhook_events", ["organization_id"])
    op.create_index("ix_payment_webhook_payment_id", "payment_webhook_events", ["payment_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_webhook_payment_id", table_name="payment_webhook_events")
    op.drop_index("ix_payment_webhook_organization_id", table_name="payment_webhook_events")
    op.drop_index("ix_payment_webhook_created_at", table_name="payment_webhook_events")
    op.drop_index("ix_payment_webhook_next_retry_at", table_name="payment_webhook_events")
    op.drop_index("ix_payment_webhook_processing_status", table_name="payment_webhook_events")
    op.drop_index("ix_payment_webhook_gateway_reference", table_name="payment_webhook_events")
    op.drop_index("ix_payment_webhook_transaction_reference", table_name="payment_webhook_events")
    op.drop_index("ix_payment_webhook_provider_event_id", table_name="payment_webhook_events")
    op.drop_index("ix_payment_webhook_provider", table_name="payment_webhook_events")
    op.drop_table("payment_webhook_events")
