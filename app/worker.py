import logging

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

        raw_text = scrape_company_website(company.domain)

        if raw_text:
            company.raw_scraped_text = raw_text
            db.commit()
        else:
            logger.warning("No content scraped for company %s (%s)", company_id, company.domain)

        pending_pitches = (
            db.query(LeadPitch)
            .filter(LeadPitch.company_id == company.id, LeadPitch.status == PitchStatus.PENDING)
            .all()
        )
        for pitch in pending_pitches:
            pitch.status = PitchStatus.PROCESSING
        db.commit()

        logger.info("Enrichment complete for company %s (%s)", company_id, company.domain)

    except Exception as exc:
        db.rollback()
        logger.exception("enrich_company_task failed for company %s", company_id)
        raise self.retry(exc=exc) from exc
    finally:
        db.close()
