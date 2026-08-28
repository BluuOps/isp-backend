from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, Protocol, TypeVar


class OltCapability(str, Enum):
    SYSTEM_INFO = "system_info"
    CARDS = "cards"
    UPLINKS = "uplinks"
    PON_PORTS = "pon_ports"
    ONUS = "onus"
    ONU_OPTICS = "onu_optics"
    ALARMS = "alarms"
    VLAN_BINDINGS = "vlan_bindings"


class ResultState(str, Enum):
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


T = TypeVar("T")


@dataclass(frozen=True)
class OltResult(Generic[T]):
    state: ResultState
    value: T | None
    observed_at: datetime | None
    error_code: str | None = None


@dataclass(frozen=True)
class AdapterContext:
    organization_id: int
    device_id: int
    management_address: str
    credential_reference_id: int | None = None
    maximum_items: int = 10_000


class OltAdapter(Protocol):
    adapter_key: str

    def capabilities(self) -> frozenset[OltCapability]: ...
    def test_connection(self, context: AdapterContext) -> OltResult[dict[str, str]]: ...
    def read_system_info(self, context: AdapterContext) -> OltResult[dict[str, object]]: ...
    def read_cards(self, context: AdapterContext) -> OltResult[list[dict[str, object]]]: ...
    def read_uplinks(self, context: AdapterContext) -> OltResult[list[dict[str, object]]]: ...
    def read_pon_ports(self, context: AdapterContext) -> OltResult[list[dict[str, object]]]: ...
    def read_onus(self, context: AdapterContext) -> OltResult[list[dict[str, object]]]: ...
    def read_onu_optics(self, context: AdapterContext, onu_key: str) -> OltResult[dict[str, object]]: ...
    def read_alarms(self, context: AdapterContext) -> OltResult[list[dict[str, object]]]: ...
    def read_vlan_bindings(self, context: AdapterContext) -> OltResult[list[dict[str, object]]]: ...
