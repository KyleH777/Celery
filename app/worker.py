import logging

from celery.exceptions import MaxRetriesExceededError, Retry

from app.ai_service import AIPermanentError, AIRetryableError, ai_service
from app.celery_app import celery_app
from app.database import SessionLocal
from app.models import Company, LeadPitch, PitchStatus
from app.scraper import scrape_company_website

logger = logging.getLogger(__name__)


@celery_app.task(
    name="tasks.enrich_company_task",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def enrich_company_task(self, company_id: str) -> None:
    db = SessionLocal()
    try:
        company = db.get(Company, company_id)
        if company is None:
            logger.error("Company %s not found", company_id)
            return

        raw_text = company.raw_scraped_text
        if not raw_text:
            raw_text = scrape_company_website(company.domain)
            if raw_text:
                company.raw_scraped_text = raw_text
                db.commit()

        pending_pitches = (
            db.query(LeadPitch)
            .filter(LeadPitch.company_id == company.id, LeadPitch.status == PitchStatus.PENDING)
            .all()
        )
        if not pending_pitches:
            logger.info("No pending pitches for company %s (%s)", company_id, company.domain)
            return

        for pitch in pending_pitches:
            pitch.status = PitchStatus.PROCESSING
        db.commit()

        if not raw_text:
            logger.error(
                "No scraped content for company %s (%s); failing pitches", company_id, company.domain
            )
            _mark_pitches(db, pending_pitches, PitchStatus.FAILED)
            return

        try:
            outreach = ai_service.generate_outreach(
                company_name=company.company_name or company.domain,
                domain=company.domain,
                raw_text=raw_text,
            )
        except AIRetryableError as exc:
            logger.warning("Retryable AI error for company %s: %s", company_id, exc)
            _mark_pitches(db, pending_pitches, PitchStatus.PENDING)
            try:
                raise self.retry(exc=exc)
            except MaxRetriesExceededError:
                logger.error("Max retries exceeded for company %s; failing pitches", company_id)
                _mark_pitches(db, pending_pitches, PitchStatus.FAILED)
                return
        except AIPermanentError as exc:
            logger.error("Permanent AI error for company %s: %s", company_id, exc)
            _mark_pitches(db, pending_pitches, PitchStatus.FAILED)
            return

        if not company.industry:
            company.industry = outreach.analysis.industry
        if not company.company_size:
            company.company_size = outreach.analysis.company_size

        analysis_data = outreach.analysis.model_dump()
        for pitch in pending_pitches:
            pitch.generated_pitch = outreach.personalized_pitch
            pitch.analysis = analysis_data
            pitch.status = PitchStatus.COMPLETED
        db.commit()

        logger.info(
            "Enrichment complete for company %s (%s): %d pitch(es) generated",
            company_id,
            company.domain,
            len(pending_pitches),
        )

    except Retry:
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("enrich_company_task failed for company %s", company_id)
        raise self.retry(exc=exc) from exc
    finally:
        db.close()


def _mark_pitches(db, pitches: list[LeadPitch], status: PitchStatus) -> None:
    for pitch in pitches:
        pitch.status = status
    db.commit()
