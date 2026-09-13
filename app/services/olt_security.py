from __future__ import annotations

from ipaddress import ip_address, ip_network

from app.core.config import settings


class ManagementAddressError(ValueError):
    pass


def validate_management_address(value: str) -> str:
    candidate = value.strip()
    try:
        address = ip_address(candidate)
    except ValueError as exc:
        raise ManagementAddressError("management_address_must_be_ip_literal") from exc
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        raise ManagementAddressError("management_address_not_permitted")
    if not address.is_private:
        raise ManagementAddressError("public_management_address_not_permitted")
    try:
        allowlist = tuple(ip_network(item, strict=False) for item in settings.olt_management_networks)
    except ValueError as exc:
        raise ManagementAddressError("management_network_allowlist_invalid") from exc
    if not any(address in network for network in allowlist):
        raise ManagementAddressError("management_address_outside_allowlist")
    return address.compressed
