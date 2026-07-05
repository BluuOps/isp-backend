import logging
import os
import subprocess
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import RadAcct, RadCheck, User
from app.schemas.radius_session import (
    RadiusDisconnectRequest,
    RadiusDisconnectResponse,
    RadiusSessionResponse,
)

logger = logging.getLogger(__name__)

RADCLIENT_BIN = os.getenv("RADIUS_RADCLIENT_BIN", "/usr/bin/radclient")
COA_SECRET_FILE = os.getenv(
    "RADIUS_COA_SECRET_FILE",
    "/etc/radiusfiber/coa.secret",
)
COA_NAS_IP = os.getenv("RADIUS_COA_NAS_IP", "192.168.222.1")
COA_PORT = os.getenv("RADIUS_COA_PORT", "3799")
PILOT_CALLED_STATION_ID = os.getenv(
    "RADIUS_PILOT_CALLED_STATION_ID",
    "core-radius-pilot",
)

REJECT_ATTRIBUTE = "Auth-Type"
REJECT_VALUE = "Reject"


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


@router.post(
    "/disconnect",
    response_model=RadiusDisconnectResponse,
    status_code=status.HTTP_200_OK,
)
def disconnect_session(
    payload: RadiusDisconnectRequest,
    db: Session = Depends(get_db),
) -> RadiusDisconnectResponse:
    account = (
        db.query(User)
        .filter(User.username == payload.username)
        .first()
    )
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PPPoE account was not found",
        )

    if account.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="PPPoE account is already disconnected or inactive",
        )

    latest_session = (
        db.query(RadAcct)
        .filter(RadAcct.username == payload.username)
        .order_by(RadAcct.acctstarttime.desc())
        .first()
    )
    if not latest_session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only validated Core RADIUS pilot accounts can be disconnected",
        )

    if latest_session.calledstationid != PILOT_CALLED_STATION_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Core RADIUS pilot accounts can be disconnected",
        )

    active_session = (
        db.query(RadAcct)
        .filter(
            RadAcct.username == payload.username,
            RadAcct.acctstoptime.is_(None),
            RadAcct.calledstationid == PILOT_CALLED_STATION_ID,
        )
        .order_by(RadAcct.acctstarttime.desc())
        .first()
    )

    if active_session:
        session_nas_ip = str(active_session.nasipaddress)
        if session_nas_ip != COA_NAS_IP:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Session NAS does not match the configured Core RADIUS NAS",
            )

        if not os.path.isfile(COA_SECRET_FILE) or not os.access(
            COA_SECRET_FILE,
            os.R_OK,
        ):
            logger.error("RADIUS CoA secret file is unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="RADIUS disconnect service is not configured",
            )

    reject = (
        db.query(RadCheck)
        .filter(
            RadCheck.username == account.username,
            RadCheck.attribute == REJECT_ATTRIBUTE,
        )
        .first()
    )
    if reject:
        reject.op = ":="
        reject.value = REJECT_VALUE
    else:
        db.add(
            RadCheck(
                username=account.username,
                attribute=REJECT_ATTRIBUTE,
                op=":=",
                value=REJECT_VALUE,
            )
        )

    account.status = "suspended"

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    if not active_session:
        return RadiusDisconnectResponse(
            username=account.username,
            session_id=latest_session.acctsessionid,
            message="PPPoE authentication blocked; no active session was present",
        )

    attributes = [
        f'User-Name = "{active_session.username}"',
        f'Acct-Session-Id = "{active_session.acctsessionid}"',
        f"NAS-IP-Address = {active_session.nasipaddress}",
    ]
    if active_session.framedipaddress:
        attributes.append(
            f"Framed-IP-Address = {active_session.framedipaddress}"
        )

    request_body = "\n".join(attributes) + "\n"

    try:
        result = subprocess.run(
            [
                RADCLIENT_BIN,
                "-S",
                COA_SECRET_FILE,
                "-x",
                f"{COA_NAS_IP}:{COA_PORT}",
                "disconnect",
            ],
            input=request_body,
            text=True,
            capture_output=True,
            timeout=12,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "RADIUS disconnect timed out for user %s",
            payload.username,
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Authentication is blocked, but the NAS did not answer the disconnect request",
        ) from exc
    except OSError as exc:
        logger.exception("Unable to execute radclient")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is blocked, but the disconnect service is unavailable",
        ) from exc

    command_output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or "Disconnect-ACK" not in command_output:
        logger.warning(
            "NAS rejected disconnect for user %s with return code %s",
            payload.username,
            result.returncode,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Authentication is blocked, but the NAS did not acknowledge the disconnect request",
        )

    logger.info(
        "Disconnected Core RADIUS user %s, session %s",
        payload.username,
        active_session.acctsessionid,
    )

    return RadiusDisconnectResponse(
        username=payload.username,
        session_id=active_session.acctsessionid,
        message="PPPoE authentication blocked and live session disconnected",
    )
