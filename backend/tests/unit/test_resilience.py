"""Retry classification, backoff and per-attempt timeouts."""

from __future__ import annotations

import asyncio

import pytest

from app.ai.resilience import (
    REASON_RATE_LIMIT,
    REASON_SERVER,
    REASON_TIMEOUT,
    backoff_delay,
    call_with_retries,
    classify_retryable,
)
from app.core.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    SchemaValidationError,
)

CALL = {"provider": "test", "tier": "simple", "model": "m", "task": "clause_extraction"}


class TestClassification:
    def test_timeouts_are_retryable(self) -> None:
        assert classify_retryable(TimeoutError()) == REASON_TIMEOUT
        assert classify_retryable(TimeoutError()) == REASON_TIMEOUT

    def test_rate_limits_are_retryable(self) -> None:
        assert classify_retryable(ProviderRateLimitError("slow down")) == REASON_RATE_LIMIT

    def test_unavailable_is_retryable(self) -> None:
        assert classify_retryable(ProviderUnavailableError("down")) == REASON_SERVER

    def test_schema_violations_are_not_retryable(self) -> None:
        """A malformed schema fails identically on every attempt."""
        assert classify_retryable(SchemaValidationError("bad json")) is None

    def test_provider_error_honours_its_own_retryable_flag(self) -> None:
        assert classify_retryable(ProviderError("x", retryable=False)) is None
        assert classify_retryable(ProviderError("x", retryable=True)) == REASON_SERVER

    def test_unknown_exceptions_are_not_retried(self) -> None:
        """Retrying an unclassified bug just makes it three times slower."""
        assert classify_retryable(ValueError("programming error")) is None


class TestBackoff:
    def test_delay_grows_with_attempt(self) -> None:
        """Ceilings grow exponentially even though the sample is jittered."""
        base = 1.0
        assert max(backoff_delay(1, base=base) for _ in range(200)) <= base
        assert max(backoff_delay(3, base=base) for _ in range(200)) <= base * 4

    def test_delay_is_jittered_not_fixed(self) -> None:
        """Lockstep retries re-trigger the very limit they back off from."""
        samples = {round(backoff_delay(4, base=1.0), 6) for _ in range(50)}
        assert len(samples) > 1

    def test_delay_is_capped(self) -> None:
        assert backoff_delay(50, base=1.0, cap=30.0) <= 30.0

    def test_delay_is_never_negative(self) -> None:
        assert all(backoff_delay(n, base=1.0) >= 0 for n in range(1, 10))


class TestCallWithRetries:
    @pytest.mark.asyncio
    async def test_returns_on_first_success(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        result, outcome = await call_with_retries(op, attempt_timeout=5, **CALL)
        assert result == "ok"
        assert calls == 1
        assert outcome.retries == 0

    @pytest.mark.asyncio
    async def test_retries_a_transient_failure_then_succeeds(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ProviderUnavailableError("try again")
            return "recovered"

        result, outcome = await call_with_retries(op, attempt_timeout=5, max_attempts=4, **CALL)
        assert result == "recovered"
        assert calls == 3
        assert outcome.retries == 2
        assert outcome.reasons == [REASON_SERVER, REASON_SERVER]

    @pytest.mark.asyncio
    async def test_does_not_retry_a_terminal_failure(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            raise SchemaValidationError("not json")

        with pytest.raises(SchemaValidationError):
            await call_with_retries(op, attempt_timeout=5, max_attempts=4, **CALL)
        assert calls == 1, "a non-retryable error must be attempted exactly once"

    @pytest.mark.asyncio
    async def test_per_attempt_timeout_is_enforced(self) -> None:
        async def op() -> str:
            await asyncio.sleep(5)
            return "never"

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await call_with_retries(op, attempt_timeout=0.05, max_attempts=1, **CALL)

    @pytest.mark.asyncio
    async def test_exhausting_attempts_raises_the_last_error(self) -> None:
        async def op() -> str:
            raise ProviderRateLimitError("always limited")

        with pytest.raises(ProviderRateLimitError):
            await call_with_retries(op, attempt_timeout=5, max_attempts=2, **CALL)

    @pytest.mark.asyncio
    async def test_deadline_stops_a_retry_that_cannot_finish(self) -> None:
        """Better to fail with the real error than time out mid-attempt."""
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            raise ProviderUnavailableError("down")

        with pytest.raises(ProviderUnavailableError):
            await call_with_retries(
                op, attempt_timeout=10, max_attempts=5, deadline_seconds=0.01, **CALL
            )
        assert calls == 1
