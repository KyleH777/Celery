"""Billing: checkout, entitlement gating, credits, and Stripe webhooks."""

from unittest.mock import patch

import app.billing_service as billing_module
import app.main as main_module
import app.webhooks as webhooks_module
from app.models import LeadPitch, PitchStatus, User
from app.worker import _mark_pitches
from tests.conftest import register_and_login


def deliver(client, event):
    """Post a webhook with the signature check mocked to return `event`."""
    with patch.object(webhooks_module.stripe.Webhook, "construct_event", return_value=event):
        return client.post(
            "/api/v1/billing/webhook", content=b"{}", headers={"stripe-signature": "sig"}
        )


class TestEntitlementGating:
    def test_enrich_requires_subscription(self, client):
        headers = register_and_login(client)
        response = client.post(
            "/api/v1/pitches/enrich", json={"domain": "acme.com"}, headers=headers
        )
        assert response.status_code == 402
        assert "subscription" in response.json()["detail"].lower()

    def test_billing_me_reports_state(self, client):
        headers = register_and_login(client)
        body = client.get("/api/v1/billing/me", headers=headers).json()
        assert body == {"subscription_status": "inactive", "lead_credits_remaining": 0}


class TestCheckout:
    def test_creates_subscription_session_and_reuses_customer(self, client):
        headers = register_and_login(client)
        with patch.object(
            billing_module.stripe.Customer, "create", return_value={"id": "cus_123"}
        ) as mock_customer, patch.object(
            billing_module.stripe.checkout.Session,
            "create",
            return_value={"url": "https://checkout.stripe.com/pay/cs_test"},
        ) as mock_session:
            first = client.post("/api/v1/billing/checkout", headers=headers)
            second = client.post("/api/v1/billing/checkout", headers=headers)

        assert first.status_code == 200
        assert first.json()["checkout_url"].startswith("https://checkout.stripe.com/")
        assert mock_session.call_args.kwargs["mode"] == "subscription"
        assert mock_session.call_args.kwargs["customer"] == "cus_123"
        assert second.status_code == 200
        assert mock_customer.call_count == 1


class TestWebhooks:
    def _activate(self, client, headers):
        with patch.object(
            billing_module.stripe.Customer, "create", return_value={"id": "cus_123"}
        ), patch.object(
            billing_module.stripe.checkout.Session,
            "create",
            return_value={"url": "https://checkout.stripe.com/x"},
        ):
            client.post("/api/v1/billing/checkout", headers=headers)
        deliver(
            client,
            {
                "id": "evt_activate",
                "type": "invoice.payment_succeeded",
                "data": {"object": {"customer": "cus_123"}},
            },
        )

    def test_bad_signature_rejected(self, client):
        response = client.post(
            "/api/v1/billing/webhook", content=b"{}", headers={"stripe-signature": "bad"}
        )
        assert response.status_code == 400

    def test_subscription_created_sets_trialing(self, client):
        headers = register_and_login(client)
        with patch.object(
            billing_module.stripe.Customer, "create", return_value={"id": "cus_123"}
        ), patch.object(
            billing_module.stripe.checkout.Session,
            "create",
            return_value={"url": "https://checkout.stripe.com/x"},
        ):
            client.post("/api/v1/billing/checkout", headers=headers)

        response = deliver(
            client,
            {
                "id": "evt_sub_1",
                "type": "customer.subscription.created",
                "data": {"object": {"customer": "cus_123", "status": "trialing"}},
            },
        )
        assert response.json()["status"] == "processed"
        me = client.get("/api/v1/billing/me", headers=headers).json()
        assert me["subscription_status"] == "trialing"

    def test_duplicate_event_processed_exactly_once(self, client):
        headers = register_and_login(client)
        self._activate(client, headers)

        event = {
            "id": "evt_dup",
            "type": "invoice.payment_succeeded",
            "data": {"object": {"customer": "cus_123"}},
        }
        assert deliver(client, event).json()["status"] == "processed"
        assert deliver(client, event).json()["status"] == "already_processed"

    def test_payment_succeeded_activates_and_provisions_credits(self, client):
        headers = register_and_login(client)
        self._activate(client, headers)
        me = client.get("/api/v1/billing/me", headers=headers).json()
        assert me["subscription_status"] == "active"
        assert me["lead_credits_remaining"] == 3  # MONTHLY_LEAD_CREDITS in tests

    def test_payment_failed_marks_past_due_and_restricts(self, client):
        headers = register_and_login(client)
        self._activate(client, headers)
        deliver(
            client,
            {
                "id": "evt_fail",
                "type": "invoice.payment_failed",
                "data": {"object": {"customer": "cus_123"}},
            },
        )
        me = client.get("/api/v1/billing/me", headers=headers).json()
        assert me["subscription_status"] == "past_due"
        response = client.post(
            "/api/v1/pitches/enrich", json={"domain": "blocked.io"}, headers=headers
        )
        assert response.status_code == 402

    def test_renewal_reactivates_and_resets_quota(self, client):
        headers = register_and_login(client)
        self._activate(client, headers)
        deliver(
            client,
            {
                "id": "evt_fail",
                "type": "invoice.payment_failed",
                "data": {"object": {"customer": "cus_123"}},
            },
        )
        deliver(
            client,
            {
                "id": "evt_renew",
                "type": "invoice.payment_succeeded",
                "data": {"object": {"customer": "cus_123"}},
            },
        )
        me = client.get("/api/v1/billing/me", headers=headers).json()
        assert me == {"subscription_status": "active", "lead_credits_remaining": 3}

    def test_unknown_customer_is_acked_not_500(self, client):
        response = deliver(
            client,
            {
                "id": "evt_ghost",
                "type": "invoice.payment_succeeded",
                "data": {"object": {"customer": "cus_nobody"}},
            },
        )
        assert response.status_code == 200

    def test_unhandled_event_types_ignored(self, client):
        response = deliver(
            client, {"id": "evt_x", "type": "charge.refunded", "data": {"object": {}}}
        )
        assert response.json()["status"] == "ignored"


class TestCreditAccounting:
    def test_credits_consumed_until_exhausted(self, client):
        headers = register_and_login(client)
        TestWebhooks()._activate(client, headers)  # 3 credits

        with patch.object(main_module.enrich_company_task, "delay"):
            codes = [
                client.post(
                    "/api/v1/pitches/enrich", json={"domain": f"c{i}.io"}, headers=headers
                ).status_code
                for i in range(4)
            ]
        assert codes == [202, 202, 202, 402]
        me = client.get("/api/v1/billing/me", headers=headers).json()
        assert me["lead_credits_remaining"] == 0

    def test_terminal_failure_refunds_credit(self, client, db_session):
        headers = register_and_login(client)
        TestWebhooks()._activate(client, headers)
        with patch.object(main_module.enrich_company_task, "delay"):
            client.post("/api/v1/pitches/enrich", json={"domain": "fail.io"}, headers=headers)

        user = db_session.query(User).filter(User.email == "user@example.com").first()
        pitch = db_session.query(LeadPitch).filter(LeadPitch.user_id == user.id).first()
        before = user.lead_credits_remaining
        _mark_pitches(db_session, [pitch], PitchStatus.FAILED)

        db_session.refresh(user)
        assert user.lead_credits_remaining == before + 1
