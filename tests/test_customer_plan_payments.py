from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.integrations.base import GatewayVerifyResult, PaymentGatewayError
from app.integrations.paystack import PaystackGateway
from app.core.tenant_host import _tenant_alias_slug
from app.schemas.payment import CustomerPaymentInitializeRequest, CustomerPaymentQuoteRequest
from app.routers.customer_portal import (
    CATALOG_CUSTOMER_STATUSES,
    PURCHASING_CUSTOMER_STATUSES,
    _select_catalog_service,
    get_customer_portal_context,
    quote_payment,
)
from app.routers.customer_auth import _account_from_token
from app.services import payment_quote
from app.services.payment_service import (
    _callback_base_url_for_organization,
    _ensure_no_pending_plan_activation,
    _gateway_initialization_error,
    _payment_quote_consumed_error,
    _stored_idempotency_key,
    process_verified_payment,
)
from app.services.subscription_renewal import process_subscription_renewal


class _GatewayResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class PaymentQuoteTests(unittest.TestCase):
    def _quote(self):
        with patch("app.services.payment_quote.settings", SimpleNamespace(jwt_secret="test-only-quote-secret")):
            return payment_quote.create_quote(
                organization_id=10,
                customer_id="customer-a",
                service_id=20,
                plan_id=30,
                billing_periods=1,
                amount_minor=500000,
                currency="NGN",
            )

    def test_quote_round_trip_binds_authoritative_fields(self):
        token, created = self._quote()
        with patch("app.services.payment_quote.settings", SimpleNamespace(jwt_secret="test-only-quote-secret")):
            decoded = payment_quote.read_quote(token)
        self.assertEqual(decoded, created)
        self.assertEqual(decoded.organization_id, 10)
        self.assertEqual(decoded.customer_id, "customer-a")
        self.assertEqual(decoded.amount_minor, 500000)

    def test_tampered_quote_is_rejected(self):
        token, _ = self._quote()
        payload, signature = token.split(".", 1)
        tampered = ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature
        with patch("app.services.payment_quote.settings", SimpleNamespace(jwt_secret="test-only-quote-secret")):
            with self.assertRaises(HTTPException) as raised:
                payment_quote.read_quote(tampered)
        self.assertEqual(raised.exception.status_code, 400)

    def test_expired_quote_is_rejected(self):
        with (
            patch("app.services.payment_quote.settings", SimpleNamespace(jwt_secret="test-only-quote-secret")),
            patch("app.services.payment_quote.time.time", return_value=1_000),
        ):
            token, _ = payment_quote.create_quote(
                organization_id=10,
                customer_id="customer-a",
                service_id=20,
                plan_id=30,
                billing_periods=1,
                amount_minor=500000,
                currency="NGN",
            )
        with (
            patch("app.services.payment_quote.settings", SimpleNamespace(jwt_secret="test-only-quote-secret")),
            patch("app.services.payment_quote.time.time", return_value=10_000),
        ):
            with self.assertRaises(HTTPException) as raised:
                payment_quote.read_quote(token)
        self.assertEqual(raised.exception.status_code, 409)


class RenewalIdempotencyTests(unittest.TestCase):
    @staticmethod
    def _payment(**overrides):
        values = {
            "renewal_processed_at": None,
            "payment_status": "successful",
            "payment_purpose": "subscription_renewal",
            "organization_id": 10,
            "customer_id": "customer-a",
            "user_id": 20,
            "selected_plan_id": None,
            "previous_plan_name": None,
            "resulting_plan_name": None,
            "old_expiration_date": None,
            "new_expiration_date": None,
            "fulfillment_status": "pending_payment",
            "gateway_metadata": {},
            "billing_periods": 1,
            "renewal_cycles": 1,
            "created_by_principal_type": "customer",
            "created_by_customer_id": "customer-a",
            "created_by": "customer@example.test",
            "transaction_reference": "TEST-REFERENCE",
            "id": 99,
            "paid_at": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_future_expiration_is_extended_once(self):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        service = SimpleNamespace(
            id=20,
            organization_id=10,
            customer_id="customer-a",
            status="active",
            expiration_date=future,
            service_plan="Tenant Plan",
        )
        payment = self._payment()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=30, organization_id=10, name="Tenant Plan", status="active"
        )
        with (
            patch("app.services.subscription_renewal.record_audit"),
            patch("app.services.subscription_renewal.cancel_stale_disconnect_jobs"),
            patch("app.services.subscription_renewal.synchronize_radius_authorization"),
        ):
            process_subscription_renewal(db, payment=payment, service=service)
            first_expiration = service.expiration_date
            process_subscription_renewal(db, payment=payment, service=service)
        self.assertEqual(first_expiration, future + timedelta(days=30))
        self.assertEqual(service.expiration_date, first_expiration)
        self.assertEqual(payment.fulfillment_status, "completed")

    def test_expired_service_renews_from_provider_paid_time(self):
        provider_paid_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        service = SimpleNamespace(
            id=20,
            organization_id=10,
            customer_id="customer-a",
            status="expired",
            expiration_date=provider_paid_at - timedelta(days=2),
            service_plan="Tenant Plan",
        )
        payment = self._payment(paid_at=provider_paid_at)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=30, organization_id=10, name="Tenant Plan", status="active"
        )
        with (
            patch("app.services.subscription_renewal.record_audit"),
            patch("app.services.subscription_renewal.cancel_stale_disconnect_jobs"),
            patch("app.services.subscription_renewal.synchronize_radius_authorization"),
        ):
            process_subscription_renewal(db, payment=payment, service=service)
        self.assertEqual(service.expiration_date, provider_paid_at + timedelta(days=30))
        self.assertEqual(service.status, "active")

    def test_suspended_service_extends_once_without_clearing_suspension(self):
        provider_paid_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        original_expiration = provider_paid_at + timedelta(days=4)
        service = SimpleNamespace(
            id=27,
            organization_id=20,
            customer_id="M41_SF_ACCEPT_SUSPENDED",
            status="suspended",
            expiration_date=original_expiration,
            service_plan="Smart Plus",
        )
        payment = self._payment(
            organization_id=20,
            customer_id="M41_SF_ACCEPT_SUSPENDED",
            user_id=27,
            paid_at=provider_paid_at,
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=31, organization_id=20, name="Smart Plus", status="active"
        )
        with (
            patch("app.services.subscription_renewal.record_audit"),
            patch("app.services.subscription_renewal.cancel_stale_disconnect_jobs"),
            patch("app.services.subscription_renewal.synchronize_radius_authorization"),
        ):
            process_subscription_renewal(db, payment=payment, service=service)
            first_expiration = service.expiration_date
            process_subscription_renewal(db, payment=payment, service=service)

        self.assertEqual(first_expiration, original_expiration + timedelta(days=30))
        self.assertEqual(service.expiration_date, first_expiration)
        self.assertEqual(service.status, "suspended")
        self.assertEqual(payment.fulfillment_status, "completed")
        self.assertIsNotNone(payment.renewal_processed_at)

    def test_plan_change_is_pending_without_service_mutation(self):
        service = SimpleNamespace(
            id=20,
            organization_id=10,
            customer_id="customer-a",
            status="active",
            expiration_date=datetime.now(timezone.utc) + timedelta(days=5),
            service_plan="Current Plan",
        )
        selected_plan = SimpleNamespace(id=31, organization_id=10, name="Upgrade Plan", duration_days=30)
        query = SimpleNamespace()
        query.filter = lambda *args, **kwargs: query
        query.first = lambda: selected_plan
        db = SimpleNamespace(query=lambda model: query)
        payment = self._payment(selected_plan_id=31)
        original_expiration = service.expiration_date
        with patch("app.services.subscription_renewal.record_audit"):
            process_subscription_renewal(db, payment=payment, service=service)
        self.assertEqual(service.service_plan, "Current Plan")
        self.assertEqual(service.expiration_date, original_expiration)
        self.assertEqual(payment.fulfillment_status, "pending_activation")
        self.assertIsNotNone(payment.renewal_processed_at)


class PaymentVerificationTests(unittest.TestCase):
    @staticmethod
    def _payment():
        return SimpleNamespace(
            id=99,
            organization_id=10,
            customer_id="customer-a",
            user_id=20,
            selected_plan_id=30,
            billing_periods=1,
            transaction_reference="RF-TEST-REFERENCE",
            expected_amount=Decimal("5000.00"),
            amount=Decimal("5000.00"),
            expected_currency="NGN",
            currency="NGN",
            payment_status="pending",
            payment_purpose="subscription_renewal",
            renewal_processed_at=None,
            renewal_cycles=1,
            previous_plan_name="Tenant Plan",
            resulting_plan_name="Tenant Plan",
            old_expiration_date=None,
            new_expiration_date=None,
            fulfillment_status="pending_payment",
            gateway_reference="RF-TEST-REFERENCE",
            raw_gateway_status=None,
            gateway_metadata={},
            paid_at=None,
            verified_at=None,
            failed_at=None,
            created_by_principal_type="customer",
            created_by_customer_id="customer-a",
            created_by="customer@example.test",
        )

    @staticmethod
    def _verification(**overrides):
        values = {
            "status": "success",
            "reference": "RF-TEST-REFERENCE",
            "amount": Decimal("5000.00"),
            "currency": "NGN",
            "gateway_reference": "123456",
            "paid_at": "2026-07-28T08:00:00Z",
            "raw_status": "success",
            "metadata": {
                "transaction_metadata": {
                    "payment_id": 99,
                    "organization_id": 10,
                    "customer_id": "customer-a",
                    "service_id": 20,
                    "plan_id": 30,
                    "billing_periods": 1,
                    "amount_minor": 500000,
                    "currency": "NGN",
                }
            },
        }
        values.update(overrides)
        return GatewayVerifyResult(**values)

    def _assert_rejected(self, verification):
        payment = self._payment()
        with (
            patch("app.services.payment_service._reject_payment") as rejected,
            self.assertRaises(HTTPException) as raised,
        ):
            process_verified_payment(SimpleNamespace(), payment=payment, verification=verification)
        self.assertEqual(raised.exception.status_code, 409)
        rejected.assert_called_once()

    def test_amount_mismatch_is_rejected(self):
        self._assert_rejected(self._verification(amount=Decimal("4999.99")))

    def test_currency_mismatch_is_rejected(self):
        self._assert_rejected(self._verification(currency="USD"))

    def test_tenant_metadata_mismatch_is_rejected(self):
        verification = self._verification()
        verification.metadata["transaction_metadata"]["organization_id"] = 11
        self._assert_rejected(verification)

    def test_plan_metadata_mismatch_is_rejected(self):
        verification = self._verification()
        verification.metadata["transaction_metadata"]["plan_id"] = 31
        self._assert_rejected(verification)

    def test_pending_success_fulfils_once_and_replay_is_idempotent(self):
        payment = self._payment()
        initial_expiration = datetime.now(timezone.utc) + timedelta(days=5)
        service = SimpleNamespace(
            id=20,
            organization_id=10,
            customer_id="customer-a",
            status="active",
            expiration_date=initial_expiration,
            service_plan="Tenant Plan",
        )
        plan = SimpleNamespace(
            id=30,
            organization_id=10,
            name="Tenant Plan",
            duration_days=30,
        )

        class Query:
            def __init__(self, result):
                self.result = result

            def filter(self, *_args, **_kwargs):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return self.result

        class Database:
            def query(self, model):
                return Query(service if model.__name__ == "User" else plan)

        with (
            patch("app.services.payment_service.record_audit"),
            patch("app.services.subscription_renewal.record_audit"),
            patch("app.services.subscription_renewal.cancel_stale_disconnect_jobs"),
            patch("app.services.subscription_renewal.synchronize_radius_authorization"),
        ):
            process_verified_payment(
                Database(),
                payment=payment,
                verification=self._verification(),
            )
            first_expiration = service.expiration_date
            process_verified_payment(
                Database(),
                payment=payment,
                verification=self._verification(),
            )

        self.assertEqual(payment.payment_status, "successful")
        self.assertEqual(payment.fulfillment_status, "completed")
        self.assertIsNotNone(payment.renewal_processed_at)
        self.assertEqual(first_expiration, initial_expiration + timedelta(days=30))
        self.assertEqual(service.expiration_date, first_expiration)


class PaymentSecurityPrimitiveTests(unittest.TestCase):
    @staticmethod
    def _context_db(*, account, organization, customer):
        db = MagicMock()

        def query(model):
            row = {
                "CustomerPortalAccount": account,
                "Organization": organization,
                "Customer": customer,
            }[model.__name__]
            result = MagicMock()
            result.filter.return_value.first.return_value = row
            return result

        db.query.side_effect = query
        return db

    def test_active_portal_identity_allows_suspended_customer_context(self):
        account = SimpleNamespace(
            id=9,
            organization_id=20,
            customer_id="M41_SF_ACCEPT_SUSPENDED",
            status="active",
        )
        organization = SimpleNamespace(id=20, status="active")
        customer = SimpleNamespace(
            id="M41_SF_ACCEPT_SUSPENDED",
            organization_id=20,
            account_status="suspended",
        )
        db = self._context_db(account=account, organization=organization, customer=customer)
        claims = {
            "sub": "9",
            "principal_type": "customer",
            "organization_id": 20,
            "customer_id": "M41_SF_ACCEPT_SUSPENDED",
        }

        with (
            patch("app.routers.customer_portal.bearer_payload", return_value=claims),
            patch(
                "app.routers.customer_portal.resolve_tenant_from_request",
                return_value=SimpleNamespace(organization=organization),
            ),
        ):
            context = get_customer_portal_context(
                request=SimpleNamespace(),
                authorization="Bearer redacted-test-token",
                db=db,
            )

        self.assertIs(context.account, account)
        self.assertIs(context.customer, customer)
        self.assertEqual(context.customer.account_status, "suspended")

    def test_customer_context_rejects_host_tenant_mismatch_without_disclosure(self):
        account = SimpleNamespace(
            id=9,
            organization_id=20,
            customer_id="M41_SF_ACCEPT_SUSPENDED",
            status="active",
        )
        organization = SimpleNamespace(id=20, status="active")
        customer = SimpleNamespace(
            id="M41_SF_ACCEPT_SUSPENDED",
            organization_id=20,
            account_status="suspended",
        )
        db = self._context_db(account=account, organization=organization, customer=customer)
        claims = {
            "sub": "9",
            "principal_type": "customer",
            "organization_id": 20,
            "customer_id": "M41_SF_ACCEPT_SUSPENDED",
        }

        with (
            patch("app.routers.customer_portal.bearer_payload", return_value=claims),
            patch(
                "app.routers.customer_portal.resolve_tenant_from_request",
                return_value=SimpleNamespace(organization=SimpleNamespace(id=21)),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            get_customer_portal_context(
                request=SimpleNamespace(),
                authorization="Bearer redacted-test-token",
                db=db,
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_disabled_portal_account_cannot_authenticate(self):
        account = SimpleNamespace(
            id=9,
            organization_id=20,
            customer_id="M41_SF_ACCEPT_SUSPENDED",
            status="disabled",
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = account
        claims = {
            "sub": "9",
            "principal_type": "customer",
            "organization_id": 20,
            "customer_id": "M41_SF_ACCEPT_SUSPENDED",
        }

        with (
            patch("app.routers.customer_auth.bearer_payload", return_value=claims),
            self.assertRaises(HTTPException) as raised,
        ):
            _account_from_token(db, "Bearer redacted-test-token")

        self.assertEqual(raised.exception.status_code, 401)

    def test_expired_and_suspended_customers_can_view_and_purchase(self):
        self.assertTrue({"active", "expired", "suspended"}.issubset(CATALOG_CUSTOMER_STATUSES))
        self.assertTrue({"active", "expired", "suspended"}.issubset(PURCHASING_CUSTOMER_STATUSES))
        self.assertTrue(
            {"terminated", "deleted", "disabled"}.isdisjoint(PURCHASING_CUSTOMER_STATUSES)
        )

    def test_suspended_customer_can_quote_owned_service_without_network_action(self):
        service = SimpleNamespace(
            id=27,
            organization_id=20,
            customer_id="M41_SF_ACCEPT_SUSPENDED",
            status="suspended",
            service_plan="Smart Plus",
            expiration_date=datetime.now(timezone.utc) + timedelta(days=4),
        )
        plan = SimpleNamespace(
            id=33,
            organization_id=20,
            name="Smart Plus",
            description="Synthetic plan",
            rate_limit="20M/10M",
            billing_interval="monthly",
            duration_days=30,
            currency="NGN",
            price_minor=2_292_400,
            status="active",
            customer_visible=True,
        )
        context = SimpleNamespace(
            organization=SimpleNamespace(id=20, status="active"),
            customer=SimpleNamespace(
                id="M41_SF_ACCEPT_SUSPENDED",
                account_status="suspended",
            ),
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = plan
        quote_record = SimpleNamespace(
            reference="M41-SUSPENDED-QUOTE",
            expires_at=int(datetime.now(timezone.utc).timestamp()) + 300,
        )

        with (
            patch("app.routers.customer_portal._customer_services", return_value=[service]),
            patch("app.routers.customer_portal._pending_plan_activation", return_value=None),
            patch(
                "app.routers.customer_portal.create_quote",
                return_value=("signed-quote-not-a-provider-transaction", quote_record),
            ) as create_quote_mock,
        ):
            response = quote_payment(
                CustomerPaymentQuoteRequest(service_id=27, plan_id=33, billing_periods=1),
                db=db,
                context=context,
            )

        self.assertEqual(response.service_id, 27)
        self.assertEqual(response.plan.id, 33)
        self.assertEqual(response.fulfillment_policy, "renewal")
        create_quote_mock.assert_called_once_with(
            organization_id=20,
            customer_id="M41_SF_ACCEPT_SUSPENDED",
            service_id=27,
            plan_id=33,
            billing_periods=1,
            amount_minor=2_292_400,
            currency="NGN",
        )

    def test_catalog_requires_explicit_service_for_multi_service_customer(self):
        services = [
            SimpleNamespace(id=20, organization_id=10, customer_id="customer-a"),
            SimpleNamespace(id=21, organization_id=10, customer_id="customer-a"),
        ]
        with self.assertRaises(HTTPException) as raised:
            _select_catalog_service(services, None)
        self.assertEqual(raised.exception.status_code, 422)

    def test_catalog_auto_selects_only_unambiguous_service(self):
        service = SimpleNamespace(id=20, organization_id=10, customer_id="customer-a")
        self.assertIs(_select_catalog_service([service], None), service)
        self.assertIsNone(_select_catalog_service([], None))

    def test_catalog_rejects_unowned_service_identifier_without_disclosure(self):
        service = SimpleNamespace(id=20, organization_id=10, customer_id="customer-a")
        with self.assertRaises(HTTPException) as raised:
            _select_catalog_service([service], 999)
        self.assertEqual(raised.exception.status_code, 404)

    def test_staging_tenant_alias_resolves_exact_synthetic_slug(self):
        with patch(
            "app.core.tenant_host.settings",
            SimpleNamespace(
                tenant_host_aliases=("smartfiber=smart-fiber-staging-acceptance",),
            ),
        ):
            self.assertEqual(
                _tenant_alias_slug("smartfiber"),
                "smart-fiber-staging-acceptance",
            )
            self.assertIsNone(_tenant_alias_slug("m41-pay-a"))

    def test_tenant_scoped_callback_uses_trusted_configured_host(self):
        configured = SimpleNamespace(
            paystack_callback_base_url="http://m41-pay-a.localhost:5182",
            paystack_callback_base_urls=(
                "smart-fiber-staging-acceptance=http://smartfiber.localhost:5182",
            ),
            tenant_allowed_domains=("localhost",),
            paystack_mode="test",
        )
        with patch("app.services.payment_service.settings", configured):
            self.assertEqual(
                _callback_base_url_for_organization("smart-fiber-staging-acceptance"),
                "http://smartfiber.localhost:5182",
            )

    def test_callback_rejects_untrusted_hostname(self):
        configured = SimpleNamespace(
            paystack_callback_base_url="https://untrusted.invalid",
            paystack_callback_base_urls=(),
            tenant_allowed_domains=("localhost",),
            paystack_mode="test",
        )
        with (
            patch("app.services.payment_service.settings", configured),
            self.assertRaises(HTTPException) as raised,
        ):
            _callback_base_url_for_organization("smart-fiber-staging-acceptance")
        self.assertEqual(raised.exception.status_code, 503)

    def test_initialization_requires_authoritative_quote(self):
        with self.assertRaises(ValueError):
            CustomerPaymentInitializeRequest(idempotency_key="browser-action-id")

    def test_idempotency_scope_binds_plan_and_quote_and_fits_column(self):
        common = {
            "organization_id": 10,
            "customer_id": "customer-" + ("x" * 100),
            "service_id": 20,
            "renewal_cycles": 1,
            "key": "browser-action-id",
        }
        first = _stored_idempotency_key(plan_id=30, quote_reference="quote-a", **common)
        second = _stored_idempotency_key(plan_id=31, quote_reference="quote-a", **common)
        third = _stored_idempotency_key(plan_id=30, quote_reference="quote-b", **common)
        self.assertLessEqual(len(first), 120)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)

    def test_consumed_quote_uses_stable_payment_specific_conflict(self):
        conflict = _payment_quote_consumed_error()
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.detail["error"], "payment_quote_consumed")
        self.assertNotIn("customer", conflict.detail["message"].lower())

    def test_pending_plan_activation_blocks_new_checkout_before_gateway(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(id=17)

        with self.assertRaises(HTTPException) as raised:
            _ensure_no_pending_plan_activation(
                db,
                organization_id=20,
                customer_id="customer-5",
                service_id=29,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["error"], "pending_plan_activation")
        self.assertNotIn("customer-5", str(raised.exception.detail))

    def test_checkout_is_allowed_without_pending_plan_activation(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        self.assertIsNone(
            _ensure_no_pending_plan_activation(
                db,
                organization_id=20,
                customer_id="customer-5",
                service_id=29,
            )
        )

    def test_gateway_initialization_error_exposes_only_safe_category(self):
        error = _gateway_initialization_error("gateway_http_400_invalid_email")
        self.assertEqual(error.status_code, 502)
        self.assertEqual(error.detail["error"], "payment_gateway_initialization_failed")
        self.assertEqual(error.detail["category"], "gateway_http_400_invalid_email")
        self.assertNotIn("@", str(error.detail))

    def test_invalid_webhook_signature_is_rejected(self):
        gateway = PaystackGateway(secret_key="validation-only-secret")
        with self.assertRaises(PaymentGatewayError) as raised:
            gateway.validate_webhook(b'{"event":"charge.success"}', "invalid")
        self.assertEqual(raised.exception.code, "invalid_signature")

    def test_initialize_sends_server_contract_with_integer_kobo(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["content_type"] = request.get_header("Content-type")
            captured["authorization_present"] = bool(request.get_header("Authorization"))
            return _GatewayResponse(
                {
                    "status": True,
                    "message": "Authorization URL created",
                    "data": {
                        "authorization_url": "https://checkout.paystack.test/synthetic",
                        "access_code": "synthetic-access",
                        "reference": "RF-20-SYNTHETIC",
                    },
                }
            )

        gateway = PaystackGateway(secret_key="validation-only-secret")
        with patch("app.integrations.paystack.urllib.request.urlopen", fake_urlopen):
            result = gateway.initialize_transaction(
                email="synthetic@example.test",
                amount_kobo=1395000,
                reference="RF-20-SYNTHETIC",
                callback_url="http://smartfiber.localhost:5182/customer/payments/return",
                metadata={"organization_id": 20, "service_id": 25, "plan_id": 35},
            )

        self.assertIsInstance(captured["payload"]["amount"], int)
        self.assertEqual(captured["payload"]["amount"], 1395000)
        self.assertEqual(
            set(captured["payload"]),
            {"email", "amount", "reference", "callback_url", "metadata"},
        )
        self.assertEqual(captured["content_type"], "application/json")
        self.assertTrue(captured["authorization_present"])
        self.assertTrue(result.authorization_url)
        self.assertTrue(result.access_code)

    def test_upstream_4xx_is_safely_classified_without_body_exposure(self):
        error = HTTPError(
            "https://api.paystack.co/transaction/initialize",
            400,
            "Bad Request",
            {},
            BytesIO(b'{"status":false,"message":"Invalid email address supplied: private@example.test"}'),
        )
        gateway = PaystackGateway(secret_key="validation-only-secret")
        with (
            patch("app.integrations.paystack.urllib.request.urlopen", side_effect=error),
            self.assertRaises(PaymentGatewayError) as raised,
        ):
            gateway.initialize_transaction(
                email="synthetic@example.test",
                amount_kobo=10000,
                reference="RF-20-SYNTHETIC",
                callback_url="http://smartfiber.localhost:5182/customer/payments/return",
                metadata={},
            )
        self.assertEqual(raised.exception.code, "gateway_http_400_invalid_email")
        self.assertNotIn("private@", str(raised.exception))

    def test_upstream_5xx_and_timeout_are_safely_classified(self):
        upstream = HTTPError(
            "https://api.paystack.co/transaction/initialize",
            503,
            "Unavailable",
            {},
            BytesIO(b'{"status":false,"message":"Service unavailable"}'),
        )
        gateway = PaystackGateway(secret_key="validation-only-secret")
        with (
            patch("app.integrations.paystack.urllib.request.urlopen", side_effect=upstream),
            self.assertRaises(PaymentGatewayError) as raised_upstream,
        ):
            gateway.initialize_transaction(
                email="synthetic@example.test",
                amount_kobo=10000,
                reference="RF-20-SYNTHETIC",
                callback_url="http://smartfiber.localhost:5182/customer/payments/return",
                metadata={},
            )
        self.assertEqual(raised_upstream.exception.code, "gateway_http_503_upstream_error")

        with (
            patch("app.integrations.paystack.urllib.request.urlopen", side_effect=TimeoutError()),
            self.assertRaises(PaymentGatewayError) as raised_timeout,
        ):
            gateway.initialize_transaction(
                email="synthetic@example.test",
                amount_kobo=10000,
                reference="RF-20-SYNTHETIC-2",
                callback_url="http://smartfiber.localhost:5182/customer/payments/return",
                metadata={},
            )
        self.assertEqual(raised_timeout.exception.code, "gateway_network_error")

    def test_unsuccessful_or_incomplete_success_body_is_rejected(self):
        gateway = PaystackGateway(secret_key="validation-only-secret")
        with (
            patch(
                "app.integrations.paystack.urllib.request.urlopen",
                return_value=_GatewayResponse({"status": False, "message": "Rejected"}),
            ),
            self.assertRaises(PaymentGatewayError) as raised_unsuccessful,
        ):
            gateway.initialize_transaction(
                email="synthetic@example.test",
                amount_kobo=10000,
                reference="RF-20-SYNTHETIC",
                callback_url="http://smartfiber.localhost:5182/customer/payments/return",
                metadata={},
            )
        self.assertEqual(raised_unsuccessful.exception.code, "gateway_initialize_failed")

        for data in (
            {"access_code": "synthetic-access"},
            {"authorization_url": "https://checkout.paystack.test/synthetic"},
        ):
            with (
                patch(
                    "app.integrations.paystack.urllib.request.urlopen",
                    return_value=_GatewayResponse({"status": True, "data": data}),
                ),
                self.assertRaises(PaymentGatewayError) as raised_incomplete,
            ):
                gateway.initialize_transaction(
                    email="synthetic@example.test",
                    amount_kobo=10000,
                    reference="RF-20-SYNTHETIC",
                    callback_url="http://smartfiber.localhost:5182/customer/payments/return",
                    metadata={},
                )
            self.assertEqual(raised_incomplete.exception.code, "gateway_invalid_response")


if __name__ == "__main__":
    unittest.main()
