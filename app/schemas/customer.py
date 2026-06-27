from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CustomerLocation(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


class CustomerBase(BaseModel):
    tenantId: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=150)
    customerType: str = Field(default="individual", pattern="^(individual|corporate)$")
    email: str = Field(..., min_length=3, max_length=150)
    phone: str = Field(..., min_length=3, max_length=50)
    address: str = Field(..., min_length=1, max_length=255)
    location: CustomerLocation
    mstId: Optional[str] = Field(default=None, max_length=100)
    splitterPort: Optional[int] = None
    fibreCoreId: Optional[str] = Field(default=None, max_length=100)
    onuSerial: str = Field(default="", max_length=100)
    oltName: str = Field(default="", max_length=100)
    ponPort: str = Field(default="", max_length=100)
    rxSignal: float = -20
    txSignal: float = 2
    accountStatus: str = Field(default="active", pattern="^(active|suspended)$")
    online: bool = False


class CustomerCreate(CustomerBase):
    id: str = Field(..., min_length=1, max_length=100)


class CustomerUpdate(CustomerBase):
    pass


class CustomerResponse(CustomerCreate):
    model_config = ConfigDict(from_attributes=True)
