"""Transactional email via Resend: enrichment-completion notifications.

Delivery is strictly best-effort: every failure path is swallowed and logged.
An email must never crash, retry-loop, or roll back the enrichment work it
reports on — the pitch data is already committed by the time we get here.
"""

import logging

import resend

from app.config import settings

logger = logging.getLogger(__name__)

resend.api_key = settings.RESEND_API_KEY


def _build_completion_html(company_count: int, dashboard_url: str) -> str:
    plural = "companies" if company_count != 1 else "company"
    # Placeholder compliance links — swap for your ESP-generated unsubscribe
    # and preference-center URLs before sending to real customers.
    unsubscribe_url = f"{dashboard_url}/email/unsubscribe"
    preferences_url = f"{dashboard_url}/email/preferences"

    return f"""\
<!DOCTYPE html>
<html lang="en">
  <body style="margin:0;padding:0;background-color:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
    <div style="display:none;max-height:0;overflow:hidden;">
      Your lead enrichment batch is ready — {company_count} {plural} analyzed.
    </div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f5f7;padding:32px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background-color:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(16,24,40,0.08);">
            <tr>
              <td style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:28px 40px;">
                <span style="font-size:20px;font-weight:700;color:#ffffff;letter-spacing:0.3px;">🎯 ProspectGPT</span>
              </td>
            </tr>
            <tr>
              <td style="padding:40px 40px 8px;">
                <h1 style="margin:0 0 12px;font-size:22px;line-height:1.3;color:#101828;">
                  Your lead batch is ready ✅
                </h1>
                <p style="margin:0 0 20px;font-size:15px;line-height:1.6;color:#475467;">
                  Good news — we finished analyzing
                  <strong style="color:#101828;">{company_count} {plural}</strong>.
                  Each one now has a structured company profile and a personalized
                  cold outreach pitch waiting for you.
                </p>
              </td>
            </tr>
            <tr>
              <td align="center" style="padding:8px 40px 36px;">
                <a href="{dashboard_url}"
                   style="display:inline-block;background-color:#4f46e5;color:#ffffff;text-decoration:none;font-size:15px;font-weight:600;padding:13px 32px;border-radius:8px;">
                  View your pitches →
                </a>
              </td>
            </tr>
            <tr>
              <td style="padding:0 40px 32px;">
                <p style="margin:0;font-size:13px;line-height:1.6;color:#98a2b3;border-top:1px solid #eaecf0;padding-top:20px;">
                  You're receiving this because you queued a lead enrichment on ProspectGPT.
                  <br>
                  <a href="{preferences_url}" style="color:#6b7280;text-decoration:underline;">Email preferences</a>
                  &nbsp;·&nbsp;
                  <a href="{unsubscribe_url}" style="color:#6b7280;text-decoration:underline;">Unsubscribe</a>
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def send_completion_email(user_email: str, company_count: int, dashboard_url: str) -> bool:
    """Notify a user that their enrichment batch finished. Returns True on success.

    Never raises: delivery problems are logged and reported via the return
    value only, so callers inside committed database flows stay unaffected.
    """
    if not settings.RESEND_API_KEY:
        logger.info(
            "RESEND_API_KEY not configured; skipping completion email to %s", user_email
        )
        return False

    plural = "companies" if company_count != 1 else "company"
    try:
        response = resend.Emails.send(
            {
                "from": settings.EMAIL_FROM,
                "to": [user_email],
                "subject": f"✅ Your {company_count} {plural} enrichment batch is complete",
                "html": _build_completion_html(company_count, dashboard_url),
                "headers": {
                    # RFC 8058 one-click unsubscribe placeholder — required by
                    # Gmail/Yahoo bulk-sender rules; point at a real endpoint
                    # before production sending.
                    "List-Unsubscribe": f"<{dashboard_url}/email/unsubscribe>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
            }
        )
    except Exception:
        logger.exception("Failed to send completion email to %s", user_email)
        return False

    logger.info(
        "Sent completion email to %s (%d %s), Resend id=%s",
        user_email,
        company_count,
        plural,
        (response or {}).get("id", "unknown"),
    )
    return True
