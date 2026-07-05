from sqlalchemy import BigInteger, Column, DateTime, Text
from sqlalchemy.dialects.postgresql import INET

from app.database import Base


class RadAcct(Base):
    __tablename__ = "radacct"

    radacctid = Column(BigInteger, primary_key=True)
    acctsessionid = Column(Text, nullable=False)
    acctuniqueid = Column(Text, nullable=False)
    username = Column(Text)
    nasipaddress = Column(INET, nullable=False)
    framedipaddress = Column(INET)
    acctstarttime = Column(DateTime(timezone=True))
    acctupdatetime = Column(DateTime(timezone=True))
    acctstoptime = Column(DateTime(timezone=True))
    acctinputoctets = Column(BigInteger)
    acctoutputoctets = Column(BigInteger)
