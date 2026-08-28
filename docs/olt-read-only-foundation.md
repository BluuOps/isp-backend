# RadiusFiber OLT read-only foundation

Migration `0014_olt_inventory_foundation` introduces tenant-scoped cached OLT
inventory. It does not implement a device protocol, polling worker, discovery,
provisioning, ONU authorization, VLAN changes, or any other operational write.

## Security boundary

- `OLT_INTEGRATION_ENABLED` defaults to `false`.
- Only IP literals inside `OLT_MANAGEMENT_NETWORKS` may be stored.
- Loopback, link-local, multicast, unspecified, public and non-allowlisted
  addresses are rejected.
- `olt_credential_references` stores only external reference metadata. It has no
  username, password, community, passphrase, private-key or resolved-secret
  column.
- The adapter registry is fixed and contains only the `null` adapter.
- The null adapter performs no DNS lookup, socket creation or subprocess call.
- Connection-test and refresh endpoints return `503` and explicitly state that
  no network connection was attempted.

## Tenant boundary

All OLT entities contain `organization_id`. Composite foreign keys prevent a
device, PON port, ONU, customer or PPPoE service from being associated across
organizations. Foreign keys into existing customer and user tables use
`RESTRICT`; no OLT deletion cascades into commercial or RADIUS data.

## Foundation access matrix

- Platform Administrators use the explicitly organization-scoped platform API.
- Organization Administrators receive all eight reviewed foundation permissions.
- NOC receives reads, disabled test/refresh requests, audit reads and association management.
- Support and Field Engineer receive inventory and telemetry reads only.
- Read Only receives inventory, telemetry and alarm reads only.
- Billing receives no OLT permission in `OLT-002A`; a future customer-linked
  association view requires its own separately reviewed least-privilege contract.
- Customer principals are denied from every internal OLT route.

## Legacy customer fields

`customers.onu_serial`, `olt_name`, `pon_port`, `rx_signal`, `tx_signal` and the
legacy online flag remain compatibility/display fields. Their defaults are not
telemetry, they are not migrated into the OLT tables, and they are not accepted
as proof of ONU identity or service association.

## Deferred work

Optical time series, alarms, VLAN bindings and retention belong to migration
`0015`. A real ZTE C320 adapter requires a separate offline-fixture gate followed
by separately authorized live read-only verification. Future OLT write
permissions must never be added to the active permission catalogue through
automatic constant discovery.
