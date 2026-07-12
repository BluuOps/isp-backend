"""Add customer portal support ticket backend objects."""

from alembic import op
import sqlalchemy as sa


revision = "0006_customer_portal_backend_mvp"
down_revision = "0005_identity_audit_boundaries"
branch_labels = None
depends_on = None


TICKET_STATUSES = ("open", "in_progress", "waiting_customer", "resolved", "closed")
TICKET_PRIORITIES = ("low", "normal", "high", "urgent")
MESSAGE_SENDERS = ("customer", "organization_staff", "system")


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("customer_id", sa.String(length=100), sa.ForeignKey("customers.id", onupdate="CASCADE", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False, server_default="general"),
        sa.Column("priority", sa.String(length=30), nullable=False, server_default="normal"),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="open"),
        sa.Column("assigned_staff_id", sa.Integer(), sa.ForeignKey("organization_staff.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN {TICKET_STATUSES}", name="ck_support_ticket_status"),
        sa.CheckConstraint(f"priority IN {TICKET_PRIORITIES}", name="ck_support_ticket_priority"),
    )
    op.create_index("ix_support_tickets_organization_id", "support_tickets", ["organization_id"])
    op.create_index("ix_support_tickets_customer_id", "support_tickets", ["customer_id"])
    op.create_index("ix_support_tickets_user_id", "support_tickets", ["user_id"])
    op.create_index("ix_support_tickets_priority", "support_tickets", ["priority"])
    op.create_index("ix_support_tickets_status", "support_tickets", ["status"])
    op.create_index("ix_support_tickets_assigned_staff_id", "support_tickets", ["assigned_staff_id"])
    op.create_index("ix_support_tickets_created_at", "support_tickets", ["created_at"])

    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("support_tickets.id", onupdate="CASCADE", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_type", sa.String(length=50), nullable=False),
        sa.Column("sender_id", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(f"sender_type IN {MESSAGE_SENDERS}", name="ck_ticket_message_sender_type"),
    )
    op.create_index("ix_ticket_messages_ticket_id", "ticket_messages", ["ticket_id"])
    op.create_index("ix_ticket_messages_created_at", "ticket_messages", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ticket_messages_created_at", table_name="ticket_messages")
    op.drop_index("ix_ticket_messages_ticket_id", table_name="ticket_messages")
    op.drop_table("ticket_messages")

    op.drop_index("ix_support_tickets_created_at", table_name="support_tickets")
    op.drop_index("ix_support_tickets_assigned_staff_id", table_name="support_tickets")
    op.drop_index("ix_support_tickets_status", table_name="support_tickets")
    op.drop_index("ix_support_tickets_priority", table_name="support_tickets")
    op.drop_index("ix_support_tickets_user_id", table_name="support_tickets")
    op.drop_index("ix_support_tickets_customer_id", table_name="support_tickets")
    op.drop_index("ix_support_tickets_organization_id", table_name="support_tickets")
    op.drop_table("support_tickets")
