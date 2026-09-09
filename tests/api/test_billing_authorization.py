"""Billing authorization must precede every provider call and mutation."""

from unittest.mock import MagicMock

import pytest

from api.models import BillingHistory, Organization, Person, Subscription
from api.security import create_access_token
from api.services.stripe_service import StripeService

pytestmark = pytest.mark.no_mock_auth

OPERATIONS = [
    ("POST", "/subscription/upgrade", {"plan_tier": "starter", "billing_cycle": "monthly"}),
    ("POST", "/subscription/trial", {"plan_tier": "starter"}),
    ("POST", "/subscription/downgrade", {"new_plan_tier": "free"}),
    ("POST", "/subscription/cancel", {}),
    ("POST", "/subscription/cancel-downgrade", None),
    ("POST", "/subscription/reactivate", None),
    ("POST", "/payment-methods", None),
    ("DELETE", "/payment-methods/pm_test", None),
    ("PUT", "/payment-methods/pm_test/primary", None),
    ("POST", "/portal", None),
    ("GET", "/payment-methods", None),
    ("GET", "/subscription", None),
    ("GET", "/history", None),
    ("GET", "/invoices/missing/pdf", None),
]


@pytest.fixture
def billing_actors(db):
    db.add_all([Organization(id="billing-a", name="A"), Organization(id="billing-b", name="B")])
    db.flush()
    actors = {}
    for identity, org_id, roles in [
        ("admin", "billing-a", ["admin"]),
        ("volunteer", "billing-a", ["volunteer"]),
        ("super_admin", "billing-a", ["super_admin"]),
        ("foreign", "billing-b", ["admin"]),
    ]:
        db.add(
            Person(
                id=identity,
                org_id=org_id,
                name=identity,
                email=f"{identity}@example.com",
                roles=roles,
            )
        )
        actors[identity] = {"Authorization": f"Bearer {create_access_token({'sub': identity})}"}
    db.commit()
    return actors


@pytest.fixture
def billing_services(monkeypatch):
    services = []
    for name, module in [
        ("StripeService", "api.services.stripe_service"),
        ("BillingService", "api.services.billing_service"),
        ("UsageService", "api.services.usage_service"),
    ]:
        service = MagicMock()
        monkeypatch.setattr(f"{module}.{name}", service)
        monkeypatch.setattr(f"api.routers.billing.{name}", service)
        services.append(service)
    return services


@pytest.mark.parametrize("method,path,body", OPERATIONS)
@pytest.mark.parametrize("caller", ["anonymous", "invalid", "volunteer", "super_admin", "foreign"])
def test_billing_denies_before_services(
    client, billing_actors, billing_services, method, path, body, caller
):
    headers = billing_actors.get(caller, {})
    if caller == "invalid":
        headers = {"Authorization": "Bearer invalid"}
    response = client.request(
        method,
        f"/api/v1/billing{path}",
        headers=headers,
        params={"org_id": "billing-a", "person_id": "admin", "payment_method_id": "pm_test"},
        json={"org_id": "billing-a", **body} if body is not None else None,
    )
    expected = {401, 403}
    if caller == "foreign" and path.startswith("/invoices/"):
        expected = {404}
    assert response.status_code in expected, response.text
    for service in billing_services:
        service.assert_not_called()


def test_portal_uses_jwt_without_identity_parameter(client, billing_actors, billing_services):
    stripe = billing_services[0]
    stripe.return_value.create_billing_portal_session.return_value = {
        "success": True,
        "url": "https://example.com/portal",
    }
    response = client.post(
        "/api/v1/billing/portal", params={"org_id": "billing-a"}, headers=billing_actors["admin"]
    )
    assert response.status_code == 200, response.text
    stripe.return_value.create_billing_portal_session.assert_called_once_with("billing-a")


@pytest.mark.parametrize("caller", ["anonymous", "invalid", "volunteer"])
def test_checkout_requires_authenticated_admin(client, billing_actors, billing_services, caller):
    headers = billing_actors.get(caller, {})
    if caller == "invalid":
        headers = {"Authorization": "Bearer invalid"}
    response = client.post(
        "/api/v1/billing/subscription/checkout-success",
        headers=headers,
        params={"session_id": "cs_test", "person_id": "admin"},
    )
    assert response.status_code in {401, 403}, response.text
    for service in billing_services:
        service.assert_not_called()


@pytest.mark.parametrize("operation", ["detach_payment_method", "set_default_payment_method"])
@pytest.mark.parametrize("customer", [None, "cus_foreign", "cus_own"])
def test_payment_method_ownership(db, billing_actors, monkeypatch, operation, customer):
    db.add(
        Subscription(
            org_id="billing-a", plan_tier="free", status="active", stripe_customer_id="cus_own"
        )
    )
    db.commit()
    retrieve = MagicMock(return_value={"customer": customer})
    detach = MagicMock()
    modify = MagicMock()
    monkeypatch.setattr("stripe.PaymentMethod.retrieve", retrieve)
    monkeypatch.setattr("stripe.PaymentMethod.detach", detach)
    monkeypatch.setattr("stripe.Customer.modify", modify)
    result = getattr(StripeService(db), operation)("billing-a", "pm_test")
    assert result["success"] is (customer == "cus_own")
    if customer != "cus_own":
        detach.assert_not_called()
        modify.assert_not_called()
    elif operation == "detach_payment_method":
        detach.assert_called_once_with("pm_test")
    else:
        modify.assert_called_once_with(
            "cus_own", invoice_settings={"default_payment_method": "pm_test"}
        )


def test_existing_foreign_invoice_is_hidden(client, db, billing_actors):
    db.add(
        BillingHistory(id=101, org_id="billing-b", event_type="charge", payment_status="succeeded")
    )
    db.commit()
    response = client.get("/api/v1/billing/invoices/101/pdf", headers=billing_actors["admin"])
    assert response.status_code == 404
    assert response.json() == {"detail": "Invoice not found"}
