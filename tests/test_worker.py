"""Worker pipeline: state machine, credit refunds, and email notifications."""

import uuid
from unittest.mock import patch

import pytest

import app.database as database
import app.worker as worker
from app.ai_service import AIPermanentError
from app.config import settings
from app.models import Company, LeadPitch, PitchStatus, User
from app.schemas_ai import CompanyAnalysis, FinalOutreach

OUTREACH = FinalOutreach(
    analysis=CompanyAnalysis(
        value_proposition="vp",
        target_audience="ta",
        industry="SaaS",
        company_size="11-50",
        pain_points=["a", "b"],
    ),
    personalized_pitch="Hi Acme...",
)


@pytest.fixture()
def seeded(db_session):
    """A user with credits, a pre-scraped company, and one pending pitch."""
    user = User(email="owner@example.com", hashed_password="x", lead_credits_remaining=10)
    db_session.add(user)
    db_session.commit()
    company = Company(
        user_id=user.id, domain="acme.com", raw_scraped_text="Acme ships fast. " * 50
    )
    db_session.add(company)
    db_session.commit()
    pitch = LeadPitch(company_id=company.id, user_id=user.id, status=PitchStatus.PENDING)
    db_session.add(pitch)
    db_session.commit()
    return {
        "user_id": str(user.id),
        "company_id": str(company.id),
        "pitch_id": pitch.id,
    }


def get_pitch(pitch_id):
    session = database.SessionLocal()
    pitch = session.get(LeadPitch, pitch_id)
    session.close()
    return pitch


class TestEnrichmentPipeline:
    def test_success_completes_pitch_and_persists_analysis(self, seeded):
        with patch.object(worker.ai_service, "generate_outreach", return_value=OUTREACH), \
             patch.object(worker.send_completion_email_task, "delay"):
            result = worker.enrich_company_task.apply(args=[seeded["company_id"]])

        assert result.successful()
        pitch = get_pitch(seeded["pitch_id"])
        assert pitch.status == PitchStatus.COMPLETED
        assert pitch.generated_pitch == "Hi Acme..."
        assert pitch.analysis["industry"] == "SaaS"

    def test_permanent_ai_error_fails_pitch_and_refunds_credit(self, seeded, db_session):
        with patch.object(
            worker.ai_service, "generate_outreach", side_effect=AIPermanentError("refused")
        ), patch.object(worker.send_completion_email_task, "delay") as mock_email:
            result = worker.enrich_company_task.apply(args=[seeded["company_id"]])

        assert result.successful()  # the task itself resolves; the pitch fails
        assert get_pitch(seeded["pitch_id"]).status == PitchStatus.FAILED
        user = db_session.get(User, uuid.UUID(seeded["user_id"]))
        assert user.lead_credits_remaining == 11  # refunded
        mock_email.assert_not_called()

    def test_malformed_company_id_dropped(self):
        result = worker.enrich_company_task.apply(args=["not-a-uuid"])
        assert result.successful()

    def test_unknown_company_dropped(self):
        result = worker.enrich_company_task.apply(args=[str(uuid.uuid4())])
        assert result.successful()


class TestCompletionEmail:
    def test_completion_enqueues_email_task(self, seeded):
        with patch.object(worker.ai_service, "generate_outreach", return_value=OUTREACH), \
             patch.object(worker.send_completion_email_task, "delay") as mock_delay:
            worker.enrich_company_task.apply(args=[seeded["company_id"]])
        mock_delay.assert_called_once_with(seeded["user_id"], 1)

    def test_email_task_resolves_user_email_from_db(self, seeded):
        with patch.object(worker, "send_completion_email", return_value=True) as mock_send:
            worker.send_completion_email_task.apply(args=[seeded["user_id"], 2])
        mock_send.assert_called_once_with(
            user_email="owner@example.com",
            company_count=2,
            dashboard_url=settings.DASHBOARD_URL,
        )

    def test_email_task_tolerates_bad_and_missing_users(self):
        assert worker.send_completion_email_task.apply(args=["not-a-uuid", 1]).successful()
        assert worker.send_completion_email_task.apply(args=[str(uuid.uuid4()), 1]).successful()

    def test_broker_failure_never_fails_enrichment(self, seeded):
        with patch.object(worker.ai_service, "generate_outreach", return_value=OUTREACH), \
             patch.object(
                 worker.send_completion_email_task,
                 "delay",
                 side_effect=ConnectionError("broker down"),
             ):
            result = worker.enrich_company_task.apply(args=[seeded["company_id"]])

        assert result.successful()
        assert get_pitch(seeded["pitch_id"]).status == PitchStatus.COMPLETED
