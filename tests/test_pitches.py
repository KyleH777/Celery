"""Pitch endpoints: queueing, polling, tenant isolation, rate limiting."""

import uuid
from unittest.mock import patch

import app.main as main_module
from app.models import LeadPitch, PitchStatus
from tests.conftest import entitle, register_and_login


def queue_pitch(client, headers, domain="acme.com"):
    with patch.object(main_module.enrich_company_task, "delay") as mock_delay:
        response = client.post(
            "/api/v1/pitches/enrich", json={"domain": domain}, headers=headers
        )
    return response, mock_delay


class TestEnrichEndpoint:
    def test_queues_pitch_and_dispatches_task(self, client, auth_headers):
        response, mock_delay = queue_pitch(client, auth_headers)
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        assert body["domain"] == "acme.com"
        mock_delay.assert_called_once_with(body["company_id"])

    def test_domain_is_normalized(self, client, auth_headers):
        response, _ = queue_pitch(client, auth_headers, domain="https://www.Acme.com/about?x=1")
        assert response.json()["domain"] == "acme.com"

    def test_invalid_domain_rejected(self, client, auth_headers):
        response, mock_delay = queue_pitch(client, auth_headers, domain="not a domain")
        assert response.status_code == 422
        mock_delay.assert_not_called()

    def test_in_flight_job_deduplicated(self, client, auth_headers):
        first, _ = queue_pitch(client, auth_headers)
        second, mock_delay = queue_pitch(client, auth_headers)
        assert second.status_code == 202
        assert second.json()["pitch_id"] == first.json()["pitch_id"]
        mock_delay.assert_not_called()


class TestGetPitch:
    def test_pending_pitch_has_null_results(self, client, auth_headers):
        queued, _ = queue_pitch(client, auth_headers)
        response = client.get(
            f"/api/v1/pitches/{queued.json()['pitch_id']}", headers=auth_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pending"
        assert body["generated_pitch"] is None
        assert body["analysis"] is None
        assert body["company"]["domain"] == "acme.com"

    def test_completed_pitch_returns_full_payload(self, client, auth_headers, db_session):
        queued, _ = queue_pitch(client, auth_headers)
        pitch = db_session.get(LeadPitch, uuid.UUID(queued.json()["pitch_id"]))
        pitch.status = PitchStatus.COMPLETED
        pitch.generated_pitch = "Hi Acme team..."
        pitch.analysis = {
            "value_proposition": "Acme helps teams ship faster",
            "target_audience": "Engineering leaders",
            "industry": "DevTools",
            "company_size": "51-200",
            "pain_points": ["slow releases", "manual toil"],
        }
        db_session.commit()

        body = client.get(
            f"/api/v1/pitches/{pitch.id}", headers=auth_headers
        ).json()
        assert body["status"] == "completed"
        assert body["generated_pitch"] == "Hi Acme team..."
        assert body["analysis"]["pain_points"] == ["slow releases", "manual toil"]

    def test_unknown_pitch_404(self, client, auth_headers):
        response = client.get(f"/api/v1/pitches/{uuid.uuid4()}", headers=auth_headers)
        assert response.status_code == 404


class TestTenantIsolation:
    def test_cross_tenant_read_is_indistinguishable_from_missing(self, client):
        alice = register_and_login(client, "alice@example.com")
        entitle("alice@example.com")
        eve = register_and_login(client, "eve@example.com")
        entitle("eve@example.com")

        queued, _ = queue_pitch(client, alice)
        pitch_id = queued.json()["pitch_id"]

        assert client.get(f"/api/v1/pitches/{pitch_id}", headers=eve).status_code == 404
        assert client.get(f"/api/v1/pitches/{pitch_id}", headers=alice).status_code == 200

    def test_same_domain_creates_separate_records_per_tenant(self, client):
        alice = register_and_login(client, "alice@example.com")
        entitle("alice@example.com")
        eve = register_and_login(client, "eve@example.com")
        entitle("eve@example.com")

        alice_pitch, _ = queue_pitch(client, alice)
        eve_pitch, _ = queue_pitch(client, eve)
        assert alice_pitch.json()["pitch_id"] != eve_pitch.json()["pitch_id"]
        assert alice_pitch.json()["company_id"] != eve_pitch.json()["company_id"]


class TestRateLimiting:
    def test_five_per_minute_per_user_then_429(self, client, auth_headers):
        main_module.limiter.enabled = True
        codes = [
            queue_pitch(client, auth_headers, domain=f"c{i}.io")[0].status_code
            for i in range(6)
        ]
        assert codes == [202, 202, 202, 202, 202, 429]

    def test_429_includes_retry_after(self, client, auth_headers):
        main_module.limiter.enabled = True
        for i in range(5):
            queue_pitch(client, auth_headers, domain=f"d{i}.io")
        response, _ = queue_pitch(client, auth_headers, domain="d6.io")
        assert response.status_code == 429
        assert response.headers.get("retry-after") == "60"

    def test_buckets_are_per_user(self, client):
        main_module.limiter.enabled = True
        alice = register_and_login(client, "alice@example.com")
        entitle("alice@example.com")
        eve = register_and_login(client, "eve@example.com")
        entitle("eve@example.com")

        for i in range(6):
            queue_pitch(client, alice, domain=f"e{i}.io")
        response, _ = queue_pitch(client, eve, domain="fresh.io")
        assert response.status_code == 202
