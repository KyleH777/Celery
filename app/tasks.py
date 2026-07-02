"""Celery background tasks: AI-powered lead pitch generation."""

import logging

from openai import OpenAI, OpenAIError

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models import Company, LeadPitch, PitchStatus

logger = logging.getLogger(__name__)

client = OpenAI(api_key=settings.OPENAI_API_KEY)


@celery_app.task(
    name="tasks.generate_lead_pitch",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def generate_lead_pitch(self, lead_pitch_id: str) -> None:
    """Generate a personalized outreach pitch for a LeadPitch and persist it.

    Runs asynchronously via a Celery worker so the API can respond
    immediately while the OpenAI call happens in the background.
    """
    db = SessionLocal()
    try:
        pitch = db.get(LeadPitch, lead_pitch_id)
        if pitch is None:
            logger.error("LeadPitch %s not found", lead_pitch_id)
            return

        pitch.status = PitchStatus.PROCESSING
        db.commit()

        company = db.get(Company, pitch.company_id)
        if company is None:
            pitch.status = PitchStatus.FAILED
            db.commit()
            logger.error("Company %s for pitch %s not found", pitch.company_id, lead_pitch_id)
            return

        prompt = (
            f"Write a concise, personalized B2B sales pitch for "
            f"{company.company_name or company.domain}, an "
            f"{company.industry or 'unknown industry'} company with "
            f"{company.company_size or 'an unknown'} employee count. "
            f"Context: {company.raw_scraped_text or 'No additional context available.'}"
        )

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an expert B2B sales copywriter."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=300,
        )

        pitch.generated_pitch = response.choices[0].message.content
        pitch.status = PitchStatus.COMPLETED
        db.commit()

    except OpenAIError as exc:
        db.rollback()
        logger.exception("OpenAI call failed for pitch %s", lead_pitch_id)
        _mark_failed(db, lead_pitch_id)
        raise self.retry(exc=exc) from exc
    except Exception:
        db.rollback()
        logger.exception("Unexpected error generating pitch %s", lead_pitch_id)
        _mark_failed(db, lead_pitch_id)
        raise
    finally:
        db.close()


def _mark_failed(db, lead_pitch_id: str) -> None:
    pitch = db.get(LeadPitch, lead_pitch_id)
    if pitch is not None:
        pitch.status = PitchStatus.FAILED
        db.commit()
