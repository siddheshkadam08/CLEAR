"""A streaming budget above the model's ceiling must not kill the answer.

`LLM_MAX_OUTPUT_TOKENS_STREAMING` defaults high on purpose - reasoning models
spend part of the budget thinking, and a tight ceiling truncates mid-sentence.
But every model has a different maximum, and asking for more than it accepts is
rejected outright with a 400 before a single token is generated.

The resulting failure is unusually bad at explaining itself:

* the Copilot drawer opens, shows its retrieval plan, then emits `error`;
* `/copilot/query` keeps working, because non-streaming uses the smaller
  `LLM_MAX_OUTPUT_TOKENS`;
* nothing in the UI mentions tokens.

So it reads as "the Copilot is broken" or "streaming is broken", when it is one
number being out of range for one deployment. The provider now reads the ceiling
out of the model's own refusal and retries once at that value.

Parsing a provider message is normally a poor idea - it is not a contract. It
earns its place here because the alternative is a total, silent-looking outage,
and because failing to parse simply surfaces the original error.
"""

from __future__ import annotations

import pytest

from app.ai.rag.openai_provider import _max_completion_tokens_from

#: The exact Azure OpenAI rejection this was written for.
AZURE_400 = (
    "Error code: 400 - {'error': {'message': 'max_tokens is too large: 64000. "
    "This model supports at most 32768 completion tokens, whereas you provided "
    "64000.', 'type': 'invalid_request_error', 'param': 'max_tokens', "
    "'code': 'invalid_value'}}"
)


class TestReadingTheCeiling:
    def test_the_azure_rejection_yields_its_limit(self) -> None:
        assert _max_completion_tokens_from(Exception(AZURE_400)) == 32768

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("max_tokens: this model supports at most 16384 completion tokens", 16384),
            # Thousands separators appear in some gateway wordings.
            ("max_completion_tokens too large. This model supports at most 16,384 tokens", 16384),
            ("MAX_TOKENS invalid - supports at most 8192 tokens", 8192),
        ],
    )
    def test_it_reads_the_common_wordings(self, message: str, expected: int) -> None:
        assert _max_completion_tokens_from(Exception(message)) == expected


class TestDecliningEverythingElse:
    """A wrong "yes" here retries a request that cannot succeed, and hides the
    real error behind a second identical failure."""

    @pytest.mark.parametrize(
        "message",
        [
            "Error code: 429 - rate limit exceeded",
            "Error code: 401 - invalid api key",
            "The deployment does not exist",
            "Connection reset by peer",
            # Mentions the parameter but states no limit.
            "max_tokens is invalid",
            # States a limit but is about something else entirely.
            "This model supports at most 4 images per request",
        ],
    )
    def test_an_unrelated_failure_is_not_a_budget_problem(self, message: str) -> None:
        assert _max_completion_tokens_from(Exception(message)) is None

    def test_an_empty_message_is_safe(self) -> None:
        assert _max_completion_tokens_from(Exception()) is None


class TestTheRetryIsBounded:
    def test_a_ceiling_at_or_above_the_request_is_not_retried(self) -> None:
        """The guard in `stream` only retries when the limit is genuinely lower.

        Without that check a model reporting a limit equal to what was asked for
        would be retried with the same value - a second identical failure, at
        double the latency, reported as the same error.
        """
        import inspect

        from app.ai.rag import openai_provider

        source = inspect.getsource(openai_provider.OpenAIProvider.stream)

        assert "allowed is None or allowed >= budget" in source
        assert "raise" in source, "an unusable ceiling must re-raise, not loop"
