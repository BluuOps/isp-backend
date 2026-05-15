from sqlalchemy import Column, Integer, String
from app.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password = Column(String, nullable=False)
    service_plan = Column(String, nullable=False)
    zone = Column(String, nullable=False)
    status = Column(String, default="inactive")

class RadCheck(Base):
    __tablename__ = "radcheck"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=False)
    attribute = Column(String, nullable=False)
    op = Column(String, nullable=False)
    value = Column(String, nullable=False)

class RadReply(Base):
    __tablename__ = "radreply"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=False)
    attribute = Column(String, nullable=False)
    op = Column(String, nullable=False)
    value = Column(String, nullable=False)
