"""Inbound Stripe webhooks: signature-verified, idempotent event processing.

Local testing with the Stripe CLI
---------------------------------
1. stripe login
2. stripe listen --forward-to localhost:8000/api/v1/billing/webhook
   (copy the printed whsec_... into STRIPE_WEBHOOK_SECRET in .env)
3. stripe trigger customer.subscription.created
   stripe trigger invoice.payment_succeeded
   stripe trigger invoice.payment_failed
"""

import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import billing_service
from app.config import settings
from app.database import get_db
from app.models import ProcessedStripeEvent, SubscriptionStatus, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

HANDLED_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "invoice.payment_succeeded",
        "invoice.payment_failed",
    }
)


def _find_user_by_customer(db: Session, customer_id: str | None) -> User | None:
    if not customer_id:
        return None
    return db.query(User).filter(User.stripe_customer_id == customer_id).first()


def _handle_subscription_change(db: Session, event: dict) -> None:
    subscription = event["data"]["object"]
    customer_id = subscription.get("customer")
    user = _find_user_by_customer(db, customer_id)

    if user is None:
        # Customers are created by us before checkout, so this should not
        # happen; ack anyway (returning 4xx would make Stripe retry forever).
        logger.error("Subscription event %s for unknown customer %s", event["id"], customer_id)
        return

    billing_service.apply_subscription_status(db, user, subscription.get("status", ""))

    if user.stripe_customer_id != customer_id:
        user.stripe_customer_id = customer_id


def _handle_payment_succeeded(db: Session, event: dict) -> None:
    invoice = event["data"]["object"]
    user = _find_user_by_customer(db, invoice.get("customer"))

    if user is None:
        logger.error("Paid invoice %s for unknown customer %s", event["id"], invoice.get("customer"))
        return

    # A paid invoice both (re)activates the account and provisions the
    # month's quota — this covers the first payment and every renewal.
    user.subscription_status = SubscriptionStatus.ACTIVE
    billing_service.grant_monthly_credits(db, user)


def _handle_payment_failed(db: Session, event: dict) -> None:
    invoice = event["data"]["object"]
    user = _find_user_by_customer(db, invoice.get("customer"))

    if user is None:
        logger.error("Failed invoice %s for unknown customer %s", event["id"], invoice.get("customer"))
        return

    # past_due is not entitled (see billing_service.ENTITLED_STATUSES), so
    # the enrichment pipeline is restricted immediately; existing credits are
    # kept so access resumes gracefully once payment is fixed.
    user.subscription_status = SubscriptionStatus.PAST_DUE
    logger.warning("User %s marked past_due after failed payment", user.id)


EVENT_HANDLERS = {
    "customer.subscription.created": _handle_subscription_change,
    "customer.subscription.updated": _handle_subscription_change,
    "invoice.payment_succeeded": _handle_payment_succeeded,
    "invoice.payment_failed": _handle_payment_failed,
}


@router.post("/webhook", summary="Stripe webhook receiver", include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    """Receive Stripe events. Authenticated by webhook signature, not JWT."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing webhooks are not configured.",
        )

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        logger.warning("Rejected webhook with invalid payload or signature")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload or signature.",
        )

    event_id = event["id"]
    event_type = event["type"]

    if event_type not in HANDLED_EVENTS:
        return {"status": "ignored", "event": event_type}

    # Idempotency, step 1: fast-path skip for events we already recorded.
    if db.get(ProcessedStripeEvent, event_id) is not None:
        logger.info("Skipping already-processed Stripe event %s", event_id)
        return {"status": "already_processed"}

    try:
        EVENT_HANDLERS[event_type](db, event)
        # Idempotency, step 2: the event ID commits in the SAME transaction
        # as the state it changed. A concurrent duplicate delivery races to
        # this commit; the loser violates the primary key and rolls back its
        # entire set of changes, so effects are applied exactly once.
        db.add(ProcessedStripeEvent(id=event_id, event_type=event_type))
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info("Duplicate concurrent delivery of Stripe event %s", event_id)
        return {"status": "already_processed"}
    except Exception:
        db.rollback()
        logger.exception("Failed to process Stripe event %s (%s)", event_id, event_type)
        # 500 makes Stripe retry with backoff — correct for transient faults.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Event processing failed.",
        )

    return {"status": "processed", "event": event_type}
