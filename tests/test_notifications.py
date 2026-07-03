"""Notification service: Resend delivery, compliance, and failure isolation."""

from unittest.mock import patch

import app.notification_service as notification_service
from app.config import settings


class TestSendCompletionEmail:
    def test_missing_api_key_is_logged_noop(self):
        with patch.object(settings, "RESEND_API_KEY", ""):
            assert (
                notification_service.send_completion_email("a@b.com", 3, "http://x") is False
            )

    def test_successful_send_builds_compliant_payload(self):
        with patch.object(settings, "RESEND_API_KEY", "re_test"), patch.object(
            notification_service.resend.Emails, "send", return_value={"id": "email_123"}
        ) as mock_send:
            ok = notification_service.send_completion_email(
                "buyer@example.com", 1, "https://app.prospectgpt.io"
            )

        assert ok is True
        payload = mock_send.call_args.args[0]
        assert payload["to"] == ["buyer@example.com"]
        assert "1 company" in payload["subject"]
        assert "https://app.prospectgpt.io" in payload["html"]
        assert "unsubscribe" in payload["html"].lower()
        assert "Email preferences" in payload["html"]
        assert payload["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_pluralization(self):
        with patch.object(settings, "RESEND_API_KEY", "re_test"), patch.object(
            notification_service.resend.Emails, "send", return_value={"id": "x"}
        ) as mock_send:
            notification_service.send_completion_email("a@b.com", 5, "http://x")
        assert "5 companies" in mock_send.call_args.args[0]["subject"]

    def test_api_failure_is_swallowed(self):
        with patch.object(settings, "RESEND_API_KEY", "re_test"), patch.object(
            notification_service.resend.Emails,
            "send",
            side_effect=RuntimeError("resend down"),
        ):
            assert (
                notification_service.send_completion_email("a@b.com", 1, "http://x") is False
            )
