from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.sql import func

from app.database import Base


class AuthTokenRevocation(Base):
    __tablename__ = "auth_token_revocations"
    __table_args__ = (
        Index("ix_auth_token_revocations_cleanup", "expires_at", "id"),
    )

    id = Column(BigInteger, primary_key=True)
    jti_hash = Column(String(64), nullable=False, unique=True, index=True)
    principal_type = Column(String(32), nullable=False, index=True)
    subject_id = Column(String(255), nullable=False, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    revoked_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(64), nullable=False, default="logout")
