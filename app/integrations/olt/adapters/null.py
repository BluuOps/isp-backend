from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.integrations.olt.contracts import AdapterContext, OltCapability, OltResult, ResultState


class NullOltAdapter:
    """Deterministic test-only adapter. It never resolves addresses or opens sockets."""

    adapter_key = "null"

    def __init__(self, mode: str = "available", observed_at: datetime | None = None):
        self.mode = mode
        self.observed_at = observed_at or datetime.now(timezone.utc)

    def capabilities(self) -> frozenset[OltCapability]:
        return frozenset(OltCapability)

    def _result(self, value):
        if self.mode == "available":
            return OltResult(ResultState.AVAILABLE, value, self.observed_at)
        if self.mode == "unsupported":
            return OltResult(ResultState.UNSUPPORTED, None, self.observed_at, "capability_unsupported")
        if self.mode == "unavailable":
            return OltResult(ResultState.UNAVAILABLE, None, self.observed_at, "adapter_disabled")
        if self.mode == "stale":
            return OltResult(ResultState.STALE, value, self.observed_at - timedelta(hours=1), "cached_result_stale")
        return OltResult(ResultState.UNAVAILABLE, None, self.observed_at, "sanitized_adapter_failure")

    def test_connection(self, context: AdapterContext):
        return self._result({"status": "simulated", "adapter": self.adapter_key})

    def read_system_info(self, context: AdapterContext):
        return self._result({"vendor": "Fixture Vendor", "model": "Fixture OLT", "software_version": "fixture"})

    def read_cards(self, context: AdapterContext):
        return self._result([{"vendor_key": "card-1", "slot": "1", "card_type": "fixture"}])

    def read_uplinks(self, context: AdapterContext):
        return self._result([{"vendor_key": "uplink-1", "name": "uplink-1"}])

    def read_pon_ports(self, context: AdapterContext):
        return self._result([{"vendor_key": "pon-1", "slot": "1", "port": "1"}])

    def read_onus(self, context: AdapterContext):
        return self._result([{"vendor_key": "onu-1", "serial_number": "FIXTURE-ONU"}])

    def read_onu_optics(self, context: AdapterContext, onu_key: str):
        return self._result({"onu_key": onu_key, "rx_dbm": None, "tx_dbm": None})

    def read_alarms(self, context: AdapterContext):
        return self._result([])

    def read_vlan_bindings(self, context: AdapterContext):
        return self._result([])
