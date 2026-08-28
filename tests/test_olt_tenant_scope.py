from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.models import OltDevice
from app.services.olt_inventory import bounded_pagination, device_or_404, list_devices


class CapturingQuery:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.expressions = []

    def filter(self, *expressions):
        self.expressions.extend(expressions)
        return self

    def count(self):
        return len(self.rows)

    def order_by(self, *args):
        return self

    def offset(self, value):
        self.offset_value = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class FakeDB:
    def __init__(self, query):
        self.query_object = query

    def query(self, model):
        self.model = model
        return self.query_object


def expression_value(expression, field):
    if getattr(getattr(expression, "left", None), "name", None) == field:
        return getattr(getattr(expression, "right", None), "value", None)
    return None


class OltTenantScopeTests(unittest.TestCase):
    def test_device_list_always_filters_by_organization(self):
        query = CapturingQuery()
        rows, total = list_devices(
            FakeDB(query),
            77,
            limit=50,
            offset=0,
            status_filter=None,
            sort_by="name",
            sort_order="asc",
        )
        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertIn(77, [expression_value(item, "organization_id") for item in query.expressions])

    def test_tenant_device_loader_returns_404(self):
        query = CapturingQuery()
        with self.assertRaises(HTTPException) as raised:
            device_or_404(FakeDB(query), 77, 991)
        self.assertEqual(raised.exception.status_code, 404)
        values = [(expression_value(item, "id"), expression_value(item, "organization_id")) for item in query.expressions]
        self.assertTrue(any(value[0] == 991 for value in values))
        self.assertTrue(any(value[1] == 77 for value in values))

    def test_pagination_and_sorting_fail_closed(self):
        with self.assertRaises(HTTPException) as raised:
            bounded_pagination(201, 0)
        self.assertEqual(raised.exception.status_code, 422)
        with self.assertRaises(HTTPException) as raised:
            list_devices(FakeDB(CapturingQuery()), 1, limit=10, offset=0, status_filter=None, sort_by="management_address;drop", sort_order="asc")
        self.assertEqual(raised.exception.status_code, 422)

    def test_device_model_is_tenant_scoped(self):
        self.assertIn("organization_id", OltDevice.__table__.columns)
        names = {constraint.name for constraint in OltDevice.__table__.constraints}
        self.assertIn("uq_olt_devices_id_org", names)
        self.assertIn("uq_olt_devices_org_name", names)
        self.assertIn("uq_olt_devices_org_address", names)


if __name__ == "__main__":
    unittest.main()
