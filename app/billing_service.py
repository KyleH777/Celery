"""Stripe billing: checkout sessions, subscription gating, and credit accounting."""

import logging
import uuid

import stripe
from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SubscriptionStatus, User

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

# Stripe subscription statuses → our internal state machine.
STRIPE_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "active": SubscriptionStatus.ACTIVE,
    "trialing": SubscriptionStatus.TRIALING,
    "past_due": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELED,
    "unpaid": SubscriptionStatus.PAST_DUE,
    "incomplete": SubscriptionStatus.INACTIVE,
    "incomplete_expired": SubscriptionStatus.CANCELED,
    "paused": SubscriptionStatus.INACTIVE,
}

ENTITLED_STATUSES = (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)


def _require_billing_configured() -> None:
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured on this deployment.",
        )


def get_or_create_stripe_customer(db: Session, user: User) -> str:
    """Return the user's Stripe customer ID, creating the customer on first use."""
    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer = stripe.Customer.create(
        email=user.email,
        metadata={"user_id": str(user.id)},
    )
    user.stripe_customer_id = customer["id"]
    db.commit()
    logger.info("Created Stripe customer %s for user %s", customer["id"], user.id)
    return customer["id"]


def create_checkout_session(db: Session, user: User, price_id: str | None = None) -> str:
    """Create a subscription Checkout Session and return its hosted URL."""
    _require_billing_configured()

    resolved_price = price_id or settings.STRIPE_PRICE_ID
    if not resolved_price:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No subscription plan is configured.",
        )

    try:
        customer_id = get_or_create_stripe_customer(db, user)
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": resolved_price, "quantity": 1}],
            client_reference_id=str(user.id),
            success_url=settings.CHECKOUT_SUCCESS_URL,
            cancel_url=settings.CHECKOUT_CANCEL_URL,
        )
    except stripe.StripeError as exc:
        # Stripe exceptions can embed request IDs and account details; log
        # them for ops, return a generic message to the client.
        logger.exception("Stripe checkout session creation failed for user %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not start checkout. Please try again.",
        ) from exc

    return session["url"]


def assert_subscription_entitled(user: User) -> None:
    """Gate for the enrichment pipeline: require an active/trialing subscription."""
    if user.subscription_status not in ENTITLED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="An active subscription is required. Start one via POST /api/v1/billing/checkout.",
        )


def try_consume_lead_credit(db: Session, user_id: uuid.UUID) -> bool:
    """Atomically reserve one lead credit; False if the balance is exhausted.

    A single conditional UPDATE (credits > 0) makes this race-safe: two
    concurrent requests against a balance of 1 cannot both succeed, because
    the database serializes the row update. The caller commits, so the
    reservation lands in the same transaction as the pitch it pays for.
    """
    result = db.execute(
        update(User)
        .where(User.id == user_id, User.lead_credits_remaining > 0)
        .values(lead_credits_remaining=User.lead_credits_remaining - 1)
    )
    return result.rowcount == 1


def refund_lead_credit(db: Session, user_id: uuid.UUID) -> None:
    """Return a reserved credit when an enrichment terminally fails."""
    db.execute(
        update(User)
        .where(User.id == user_id)
        .values(lead_credits_remaining=User.lead_credits_remaining + 1)
    )
    logger.info("Refunded 1 lead credit to user %s after failed enrichment", user_id)


def grant_monthly_credits(db: Session, user: User) -> None:
    """Reset the user's quota to the plan allowance (called on paid invoices)."""
    user.lead_credits_remaining = settings.MONTHLY_LEAD_CREDITS
    logger.info(
        "Granted %d monthly lead credits to user %s", settings.MONTHLY_LEAD_CREDITS, user.id
    )


def apply_subscription_status(db: Session, user: User, stripe_status: str) -> None:
    """Map and persist a Stripe subscription status onto the user."""
    new_status = STRIPE_STATUS_MAP.get(stripe_status)
    if new_status is None:
        logger.warning("Unknown Stripe subscription status %r for user %s", stripe_status, user.id)
        return
    user.subscription_status = new_status
    logger.info("User %s subscription status -> %s", user.id, new_status.value)
