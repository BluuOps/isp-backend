from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import threading

import pytest
from fastapi import HTTPException

from app.core.authorization import Permission, require_permission, role_permissions
from app.models import PaymentTransaction, ServicePlan, User
from app.routers.plan_activations import activate_pending_plan_change
from app.schemas.plan_activation import PlanActivationRequest
from app.services.plan_activation import (
    _eligibility,
    _proposed_expiration,
    activate_plan_change,
    resolve_duplicate_plan_payments,
)
from app.services.plan_activation_identity import logical_plan_purchase_key


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def payment(**overrides):
    values = {
        "id": 17,
        "activation_status": "pending_activation",
        "payment_status": "successful",
        "verified_at": NOW,
        "fulfillment_status": "pending_activation",
        "resolution_status": "unresolved",
        "purchased_duration_days": 30,
        "billing_periods": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def service(**overrides):
    values = {"status": "active", "expiration_date": NOW + timedelta(days=5)}
    values.update(overrides)
    return SimpleNamespace(**values)


def key(*, service_id=29, selected_plan_id=101, expiration=NOW, periods=1):
    return logical_plan_purchase_key(
        organization_id=20,
        customer_id="M41_SF_ACCEPT_ACCOUNT_5",
        service_id=service_id,
        source_plan_name="Smart Premium",
        selected_plan_id=selected_plan_id,
        source_expiration=expiration,
        billing_periods=periods,
    )


def test_duplicate_attempts_share_one_logical_period_key():
    assert key() == key()


def test_independent_service_and_plan_have_distinct_period_keys():
    assert key(service_id=28) != key(service_id=29)
    assert key(selected_plan_id=102) != key(selected_plan_id=101)


def test_future_expiration_is_extended_without_lost_time():
    assert _proposed_expiration(payment(), service(), NOW) == NOW + timedelta(days=35)


def test_expired_service_starts_from_activation_time():
    expired = service(expiration_date=NOW - timedelta(days=2))
    assert _proposed_expiration(payment(), expired, NOW) == NOW + timedelta(days=30)


def test_billing_periods_are_applied_once():
    assert _proposed_expiration(payment(billing_periods=2), service(expiration_date=None), NOW) == NOW + timedelta(days=60)


def test_suspended_service_can_be_staff_activated_without_status_change():
    suspended = service(status="suspended")
    assert _eligibility(payment(), suspended, [payment()]) == (True, None)
    assert suspended.status == "suspended"


def test_terminated_or_unverified_payment_is_blocked():
    assert _eligibility(payment(), service(status="terminated"), [payment()]) == (False, "service_status_not_activatable")
    assert _eligibility(payment(verified_at=None), service(), [payment()]) == (False, "payment_not_verified_successful")


def test_unresolved_duplicates_require_explicit_canonical_selection():
    first = payment(id=17)
    second = payment(id=18)
    assert _eligibility(first, service(), [first, second]) == (False, "duplicate_payment_ambiguity")


def test_only_canonical_duplicate_is_eligible():
    first = payment(id=17, resolution_status="canonical")
    second = payment(id=18, activation_status="blocked_duplicate", resolution_status="duplicate")
    assert _eligibility(first, service(), [first, second]) == (True, None)
    assert _eligibility(second, service(), [first, second]) == (False, "non_canonical_duplicate")


def test_activation_permission_is_restricted_to_approved_staff_roles():
    assert Permission.PAYMENTS_PLAN_ACTIVATE in role_permissions("Organization Admin")
    assert Permission.PAYMENTS_PLAN_ACTIVATE in role_permissions("Billing")
    assert Permission.PAYMENTS_PLAN_ACTIVATE not in role_permissions("Read Only")
    assert Permission.PAYMENTS_PLAN_ACTIVATE not in role_permissions("Support")
    read_only = SimpleNamespace(has_permission=lambda _permission: False)
    with pytest.raises(HTTPException) as exc:
        require_permission(Permission.PAYMENTS_PLAN_ACTIVATE)(read_only)
    assert exc.value.status_code == 403


def test_duplicate_resolution_requires_explicit_canonical_selection_and_does_not_activate():
    first = activation_evidence(id=17, user_id=29, selected_plan_id=101)
    second = activation_evidence(id=18, user_id=29, selected_plan_id=101)
    db = FakeSession(first, None, None)
    with (
        patch("app.services.plan_activation._competitors", return_value=[first, second]),
        patch("app.services.plan_activation.record_audit") as audit,
    ):
        canonical, duplicates = resolve_duplicate_plan_payments(
            db,
            PRINCIPAL,
            payment_id=17,
            canonical_payment_id=18,
            correlation_id="explicit-resolution-correlation",
            reason="Customer and billing review selected payment 18",
        )
    assert canonical.id == 18
    assert duplicates == [17]
    assert first.resolution_status == "duplicate"
    assert first.activation_status == "blocked_duplicate"
    assert second.resolution_status == "canonical"
    assert second.activation_status == "pending_activation"
    assert second.activated_at is None
    assert [call.kwargs["action"] for call in audit.call_args_list] == [
        "plan_activation.duplicate_recorded", "plan_activation.canonical_selected",
    ]


class QueryResult:
    def __init__(self, value):
        self.value = value
        self.locked = False

    def filter(self, *_args):
        return self

    def with_for_update(self):
        self.locked = True
        return self

    def first(self):
        return self.value


class FakeSession:
    def __init__(self, selected_payment, selected_service, selected_plan):
        self.results = {
            PaymentTransaction: QueryResult(selected_payment),
            User: QueryResult(selected_service),
            ServicePlan: QueryResult(selected_plan),
        }
        self.flush = MagicMock()

    def query(self, model):
        return self.results[model]


def activation_evidence(**overrides):
    values = {
        "id": 23,
        "organization_id": 20,
        "customer_id": "M41_SF_ACCEPT_ACCOUNT_5",
        "user_id": 28,
        "selected_plan_id": 102,
        "activation_period_key": key(service_id=28, selected_plan_id=102),
        "activation_status": "pending_activation",
        "activation_correlation_id": None,
        "activated_at": None,
        "activated_by_staff_id": None,
        "resolution_status": "unresolved",
        "resolution_reason": None,
        "resolved_at": None,
        "resolved_by_staff_id": None,
        "canonical_payment_id": None,
        "payment_status": "successful",
        "verified_at": NOW,
        "fulfillment_status": "pending_activation",
        "previous_plan_name": "Smart Basic",
        "purchased_duration_days": 30,
        "billing_periods": 1,
        "expected_amount": "18980.00",
        "amount": "18980.00",
        "expected_currency": "NGN",
        "currency": "NGN",
        "new_expiration_date": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


PRINCIPAL = SimpleNamespace(organization_id=20, subject_id="staff:7", actor_label="billing@tenant.test")


def test_activation_updates_only_selected_service_and_preserves_suspension():
    evidence = activation_evidence()
    selected_service = SimpleNamespace(
        id=28, organization_id=20, customer_id=evidence.customer_id,
        service_plan="Smart Basic", expiration_date=NOW + timedelta(days=5), status="suspended",
    )
    other_service = SimpleNamespace(id=29, service_plan="Smart Premium", expiration_date=NOW + timedelta(days=10), status="active")
    selected_plan = SimpleNamespace(id=102, organization_id=20, name="Smart Flex", status="active")
    db = FakeSession(evidence, selected_service, selected_plan)

    with (
        patch("app.services.plan_activation._now", return_value=NOW),
        patch("app.services.plan_activation._competitors", return_value=[evidence]),
        patch("app.services.plan_activation._summary", return_value=SimpleNamespace(payment_id=23)) as summary,
        patch("app.services.plan_activation.record_audit") as audit,
    ):
        result = activate_plan_change(db, PRINCIPAL, payment_id=23, correlation_id="activation-correlation-23")

    assert result.payment_id == 23
    assert selected_service.service_plan == "Smart Flex"
    assert selected_service.expiration_date == NOW + timedelta(days=35)
    assert selected_service.status == "suspended"
    assert (other_service.service_plan, other_service.expiration_date, other_service.status) == (
        "Smart Premium", NOW + timedelta(days=10), "active",
    )
    assert evidence.activation_status == "activated"
    assert evidence.fulfillment_status == "completed"
    assert evidence.activation_correlation_id == "activation-correlation-23"
    assert db.results[PaymentTransaction].locked is True
    assert db.results[User].locked is True
    assert [call.kwargs["action"] for call in audit.call_args_list] == [
        "plan_activation.attempted", "plan_activation.completed",
    ]
    summary.assert_called_once()
    db.flush.assert_called_once()


def test_exact_replay_returns_existing_result_but_conflicting_replay_is_409():
    evidence = activation_evidence(
        activation_status="activated", fulfillment_status="completed",
        activation_correlation_id="same-correlation", new_expiration_date=NOW + timedelta(days=30),
    )
    db = FakeSession(evidence, None, None)
    with (
        patch("app.services.plan_activation._summary", return_value=SimpleNamespace(payment_id=23)),
        patch("app.services.plan_activation.record_audit"),
    ):
        assert activate_plan_change(db, PRINCIPAL, payment_id=23, correlation_id="same-correlation").payment_id == 23
    with pytest.raises(HTTPException) as exc, patch("app.services.plan_activation.record_audit"):
        activate_plan_change(db, PRINCIPAL, payment_id=23, correlation_id="different-correlation")
    assert exc.value.status_code == 409


def test_cross_tenant_payment_is_hidden_as_404():
    db = FakeSession(None, None, None)
    with pytest.raises(HTTPException) as exc:
        activate_plan_change(db, PRINCIPAL, payment_id=999, correlation_id="cross-tenant-request")
    assert exc.value.status_code == 404


class LockedQuery(QueryResult):
    def __init__(self, value, lock, session):
        super().__init__(value)
        self.lock = lock
        self.session = session

    def with_for_update(self):
        self.lock.acquire()
        self.session.held.append(self.lock)
        return super().with_for_update()


class ConcurrentSession(FakeSession):
    def __init__(self, selected_payment, selected_service, selected_plan, locks):
        self.held = []
        self.results = {
            PaymentTransaction: LockedQuery(selected_payment, locks[PaymentTransaction], self),
            User: LockedQuery(selected_service, locks[User], self),
            ServicePlan: QueryResult(selected_plan),
        }
        self.flush = MagicMock()

    def commit(self):
        while self.held:
            self.held.pop().release()

    def rollback(self):
        self.commit()


def test_two_concurrent_exact_requests_change_service_once():
    evidence = activation_evidence()
    selected_service = SimpleNamespace(
        id=28, organization_id=20, customer_id=evidence.customer_id,
        service_plan="Smart Basic", expiration_date=NOW, status="active",
    )
    selected_plan = SimpleNamespace(id=102, organization_id=20, name="Smart Flex", status="active")
    locks = {PaymentTransaction: threading.Lock(), User: threading.Lock()}
    sessions = [ConcurrentSession(evidence, selected_service, selected_plan, locks) for _ in range(2)]
    results = []
    failures = []

    def run(session):
        try:
            results.append(activate_pending_plan_change(
                23,
                PlanActivationRequest(correlation_id="shared-concurrent-correlation"),
                db=session,
                principal=PRINCIPAL,
            ))
        except Exception as error:  # pragma: no cover - asserted below
            failures.append(error)

    with (
        patch("app.services.plan_activation._now", return_value=NOW),
        patch("app.services.plan_activation._competitors", return_value=[evidence]),
        patch("app.services.plan_activation._summary", return_value=SimpleNamespace(payment_id=23)),
        patch("app.services.plan_activation.record_audit"),
    ):
        threads = [threading.Thread(target=run, args=(session,)) for session in sessions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

    assert not failures
    assert len(results) == 2
    assert selected_service.service_plan == "Smart Flex"
    assert selected_service.expiration_date == NOW + timedelta(days=30)
    assert evidence.activation_status == "activated"
