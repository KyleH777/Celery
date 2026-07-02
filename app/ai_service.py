import logging

from openai import (
    APIConnectionError,
    APIStatusError,
    BadRequestError,
    LengthFinishReasonError,
    OpenAI,
    RateLimitError,
)

from app.config import settings
from app.schemas_ai import CompanyAnalysis, FinalOutreach

logger = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"
MAX_INPUT_CHARS = 48_000
TRUNCATION_STEP = 12_000
MIN_INPUT_CHARS = 4_000


class AIServiceError(Exception):
    pass


class AIRetryableError(AIServiceError):
    pass


class AIPermanentError(AIServiceError):
    pass


class AIService:
    def __init__(self, client: OpenAI | None = None) -> None:
        self.client = client or OpenAI(api_key=settings.OPENAI_API_KEY)

    def generate_outreach(self, company_name: str, domain: str, raw_text: str) -> FinalOutreach:
        if not raw_text or not raw_text.strip():
            raise AIPermanentError(f"No scraped text available for {domain}")

        text = raw_text[:MAX_INPUT_CHARS]

        analysis = self._analyze_company(company_name, domain, text)
        logger.info("Analysis complete for %s: %d pain points", domain, len(analysis.pain_points))

        pitch = self._draft_pitch(company_name, domain, text, analysis)
        return FinalOutreach(analysis=analysis, personalized_pitch=pitch)

    def _analyze_company(self, company_name: str, domain: str, text: str) -> CompanyAnalysis:
        system = (
            "You are a B2B market analyst. Analyze the provided website text and "
            "extract the company's core value proposition, target audience, and "
            "2-3 likely business pain points they face. Be specific and grounded "
            "in the text; do not invent details."
        )
        user = f"Company: {company_name} ({domain})\n\nWebsite text:\n{text}"
        return self._parse_with_backoff(system, user, CompanyAnalysis, domain)

    def _draft_pitch(
        self, company_name: str, domain: str, text: str, analysis: CompanyAnalysis
    ) -> str:
        system = (
            "You are an expert B2B cold outreach copywriter. Write a concise "
            "(under 150 words) cold outreach email using the Problem-Agitate-Solve "
            "framework:\n"
            "1. Problem: open with one of their likely pain points.\n"
            "2. Agitate: briefly amplify the cost of ignoring it.\n"
            "3. Solve: position our lead-enrichment solution as the fix.\n"
            "You MUST reference at least one specific, verifiable detail from their "
            "website text (a product name, headline, claim, or phrase) so the reader "
            "knows this is not automated spam. No placeholders like [Name]. "
            "Return the analysis you were given unchanged, plus the pitch."
        )
        user = (
            f"Company: {company_name} ({domain})\n\n"
            f"Analysis:\n"
            f"- Value proposition: {analysis.value_proposition}\n"
            f"- Target audience: {analysis.target_audience}\n"
            f"- Pain points: {'; '.join(analysis.pain_points)}\n\n"
            f"Website text (for specific details):\n{text}"
        )
        result = self._parse_with_backoff(system, user, FinalOutreach, domain)
        return result.personalized_pitch

    def _parse_with_backoff(
        self,
        system: str,
        user: str,
        response_model: type[CompanyAnalysis] | type[FinalOutreach],
        domain: str,
    ) -> CompanyAnalysis | FinalOutreach:
        prompt = user
        while True:
            try:
                completion = self.client.beta.chat.completions.parse(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    response_format=response_model,
                    temperature=0.7,
                )
                parsed = completion.choices[0].message.parsed
                if parsed is None:
                    refusal = completion.choices[0].message.refusal
                    raise AIPermanentError(f"Model refused or returned no parse for {domain}: {refusal}")
                return parsed

            except LengthFinishReasonError as exc:
                prompt = self._shrink(prompt, domain)
                if prompt is None:
                    raise AIPermanentError(f"Output truncated even at minimum input for {domain}") from exc

            except BadRequestError as exc:
                if "context_length" in str(exc) or "maximum context" in str(exc):
                    prompt = self._shrink(prompt, domain)
                    if prompt is None:
                        raise AIPermanentError(f"Context window exceeded even at minimum input for {domain}") from exc
                else:
                    raise AIPermanentError(f"Bad request for {domain}: {exc}") from exc

            except RateLimitError as exc:
                logger.warning("OpenAI rate limit hit for %s", domain)
                raise AIRetryableError(f"Rate limited for {domain}") from exc

            except APIConnectionError as exc:
                logger.warning("OpenAI connection error for %s: %s", domain, exc)
                raise AIRetryableError(f"Connection error for {domain}") from exc

            except APIStatusError as exc:
                if exc.status_code >= 500:
                    logger.warning("OpenAI server error %d for %s", exc.status_code, domain)
                    raise AIRetryableError(f"Server error {exc.status_code} for {domain}") from exc
                raise AIPermanentError(f"API error {exc.status_code} for {domain}: {exc}") from exc

    @staticmethod
    def _shrink(prompt: str, domain: str) -> str | None:
        new_length = len(prompt) - TRUNCATION_STEP
        if new_length < MIN_INPUT_CHARS:
            return None
        logger.warning("Truncating prompt for %s to %d chars", domain, new_length)
        return prompt[:new_length]


ai_service = AIService()
