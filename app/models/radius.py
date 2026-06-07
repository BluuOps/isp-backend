from sqlalchemy import Column, Integer, String

from app.database import Base


class RadCheck(Base):
    __tablename__ = "radcheck"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), nullable=False, index=True)
    attribute = Column(String(64), nullable=False)
    op = Column(String(2), nullable=False, default=":=")
    value = Column(String(253), nullable=False)


class RadReply(Base):
    __tablename__ = "radreply"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), nullable=False, index=True)
    attribute = Column(String(64), nullable=False)
    op = Column(String(2), nullable=False, default=":=")
    value = Column(String(253), nullable=False)
