from datetime import datetime
from typing import Optional

from pydantic import BaseModel


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
