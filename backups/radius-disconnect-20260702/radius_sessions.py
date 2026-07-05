from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import RadAcct
from app.schemas import RadiusSessionResponse


router = APIRouter(prefix="/radius", tags=["RADIUS Sessions"])


@router.get("/sessions", response_model=List[RadiusSessionResponse])
def list_active_sessions(db: Session = Depends(get_db)) -> list[RadiusSessionResponse]:
    rows = (
        db.query(RadAcct)
        .filter(RadAcct.acctstoptime.is_(None))
        .order_by(RadAcct.acctstarttime.desc())
        .limit(500)
        .all()
    )

    return [
        RadiusSessionResponse(
            id=row.radacctid,
            session_id=row.acctsessionid,
            username=row.username or "",
            nas_ip_address=str(row.nasipaddress),
            framed_ip_address=str(row.framedipaddress) if row.framedipaddress else None,
            started_at=row.acctstarttime,
            updated_at=row.acctupdatetime,
            input_octets=row.acctinputoctets or 0,
            output_octets=row.acctoutputoctets or 0,
        )
        for row in rows
    ]
