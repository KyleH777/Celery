"""AI service: structured-output chain, error taxonomy, context management."""

from unittest.mock import MagicMock

import httpx
import openai
import pytest

from app.ai_service import AIPermanentError, AIRetryableError, AIService
from app.schemas_ai import CompanyAnalysis, FinalOutreach

REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def make_analysis() -> CompanyAnalysis:
    return CompanyAnalysis(
        value_proposition="Acme helps teams ship faster",
        target_audience="Engineering leaders",
        industry="DevTools",
        company_size="51-200",
        pain_points=["slow releases", "manual toil"],
    )


def completion_with(parsed) -> MagicMock:
    return MagicMock(choices=[MagicMock(message=MagicMock(parsed=parsed, refusal=None))])


@pytest.fixture()
def service():
    client = MagicMock()
    return AIService(client=client), client


class TestHappyPath:
    def test_two_step_chain_produces_outreach(self, service):
        svc, client = service
        analysis = make_analysis()
        outreach = FinalOutreach(analysis=analysis, personalized_pitch="Hi Acme...")
        client.beta.chat.completions.parse.side_effect = [
            completion_with(analysis),
            completion_with(outreach),
        ]

        result = svc.generate_outreach("Acme", "acme.com", "Acme ships fast. " * 20)

        assert result.personalized_pitch == "Hi Acme..."
        assert result.analysis.industry == "DevTools"
        assert client.beta.chat.completions.parse.call_count == 2

    def test_pitch_prompt_receives_extracted_insights(self, service):
        svc, client = service
        analysis = make_analysis()
        outreach = FinalOutreach(analysis=analysis, personalized_pitch="p")
        client.beta.chat.completions.parse.side_effect = [
            completion_with(analysis),
            completion_with(outreach),
        ]

        svc.generate_outreach("Acme", "acme.com", "site text")

        second_call = client.beta.chat.completions.parse.call_args_list[1]
        user_message = second_call.kwargs["messages"][1]["content"]
        assert "slow releases" in user_message
        assert "Engineering leaders" in user_message


class TestErrorTaxonomy:
    def test_empty_text_is_permanent(self, service):
        svc, _ = service
        with pytest.raises(AIPermanentError):
            svc.generate_outreach("Acme", "acme.com", "   ")

    def test_rate_limit_is_retryable(self, service):
        svc, client = service
        client.beta.chat.completions.parse.side_effect = openai.RateLimitError(
            "rl", response=httpx.Response(429, request=REQUEST), body=None
        )
        with pytest.raises(AIRetryableError):
            svc.generate_outreach("Acme", "acme.com", "some text")

    def test_connection_error_is_retryable(self, service):
        svc, client = service
        client.beta.chat.completions.parse.side_effect = openai.APIConnectionError(
            request=REQUEST
        )
        with pytest.raises(AIRetryableError):
            svc.generate_outreach("Acme", "acme.com", "some text")

    def test_server_error_is_retryable(self, service):
        svc, client = service
        client.beta.chat.completions.parse.side_effect = openai.APIStatusError(
            "boom", response=httpx.Response(503, request=REQUEST), body=None
        )
        with pytest.raises(AIRetryableError):
            svc.generate_outreach("Acme", "acme.com", "some text")

    def test_client_error_is_permanent(self, service):
        svc, client = service
        client.beta.chat.completions.parse.side_effect = openai.APIStatusError(
            "nope", response=httpx.Response(403, request=REQUEST), body=None
        )
        with pytest.raises(AIPermanentError):
            svc.generate_outreach("Acme", "acme.com", "some text")

    def test_refusal_is_permanent(self, service):
        svc, client = service
        refusal_message = MagicMock(parsed=None, refusal="I can't help with that")
        client.beta.chat.completions.parse.return_value = MagicMock(
            choices=[MagicMock(message=refusal_message)]
        )
        with pytest.raises(AIPermanentError):
            svc.generate_outreach("Acme", "acme.com", "some text")


class TestContextWindowManagement:
    def test_overflow_shrinks_then_fails_permanently(self, service):
        svc, client = service
        client.beta.chat.completions.parse.side_effect = openai.BadRequestError(
            "This model's maximum context length is exceeded",
            response=httpx.Response(400, request=REQUEST),
            body=None,
        )
        with pytest.raises(AIPermanentError, match="[Cc]ontext"):
            svc.generate_outreach("Acme", "acme.com", "x" * 60_000)
        # Shrinks in steps before giving up: more than one attempt was made.
        assert client.beta.chat.completions.parse.call_count > 1

    def test_input_capped_before_first_call(self, service):
        svc, client = service
        analysis = make_analysis()
        outreach = FinalOutreach(analysis=analysis, personalized_pitch="p")
        client.beta.chat.completions.parse.side_effect = [
            completion_with(analysis),
            completion_with(outreach),
        ]

        svc.generate_outreach("Acme", "acme.com", "x" * 100_000)

        first_call = client.beta.chat.completions.parse.call_args_list[0]
        user_message = first_call.kwargs["messages"][1]["content"]
        assert len(user_message) < 50_000
