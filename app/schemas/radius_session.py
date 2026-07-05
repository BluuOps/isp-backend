from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RadiusSessionResponse(BaseModel):
    id: int
    session_id: str
    username: str
    nas_ip_address: str
    framed_ip_address: Optional[str] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    input_octets: int = 0
    output_octets: int = 0

class RadiusDisconnectRequest(BaseModel):
    username: str = Field(min_length=1, max_length=253)


class RadiusDisconnectResponse(BaseModel):
    username: str
    session_id: str
    message: str

class RadiusDisconnectRequest(BaseModel):
    username: str = Field(
        min_length=1,
        max_length=253,
        pattern=r"^[A-Za-z0-9_.@-]+$",
    )
