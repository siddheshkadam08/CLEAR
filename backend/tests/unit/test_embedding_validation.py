"""Vector validation, provider resolution and configuration diagnostics."""

from __future__ import annotations

import math
from typing import Any

import pytest

from app.ai.embedding.diagnostics import HNSW_MAX_DIMS, check_configuration
from app.ai.embedding.providers import IEmbeddingProvider, validate_vector
from app.core.errors import ProviderError


# =============================================================================
# validate_vector
# =============================================================================
def test_accepts_a_well_formed_vector() -> None:
    assert validate_vector([0.1, 0.2, 0.3], 3) == [0.1, 0.2, 0.3]


def test_accepts_integers_as_floats() -> None:
    assert validate_vector([1, 0, -1], 3) == [1.0, 0.0, -1.0]


def test_rejects_a_null_vector() -> None:
    with pytest.raises(ProviderError, match="null"):
        validate_vector(None, 3)


def test_rejects_a_non_array() -> None:
    with pytest.raises(ProviderError, match="not an array"):
        validate_vector("[0.1,0.2]", 3)


def test_rejects_the_wrong_width() -> None:
    with pytest.raises(ProviderError, match="expected 4 dimensions, got 3"):
        validate_vector([0.1, 0.2, 0.3], 4)


def test_wrong_width_message_rules_out_padding() -> None:
    """The message has to say the vector is not resized, because that is the
    assumption an operator otherwise makes when they see a dimension error."""
    with pytest.raises(ProviderError) as caught:
        validate_vector([0.1], 4)
    assert "never truncated or padded" in str(caught.value)


def test_rejects_a_null_element() -> None:
    with pytest.raises(ProviderError, match="null element"):
        validate_vector([0.1, None, 0.3], 3)


def test_rejects_a_non_numeric_element() -> None:
    with pytest.raises(ProviderError, match="non-numeric"):
        validate_vector([0.1, "x", 0.3], 3)


def test_rejects_a_boolean_element() -> None:
    """`True` is an int in Python. Accepting it would store 1.0 for a field that
    was never meant to be numeric."""
    with pytest.raises(ProviderError, match="non-numeric"):
        validate_vector([0.1, True, 0.3], 3)


def test_rejects_nan() -> None:
    with pytest.raises(ProviderError, match="NaN"):
        validate_vector([0.1, math.nan, 0.3], 3)


def test_rejects_positive_infinity() -> None:
    with pytest.raises(ProviderError, match="infinity"):
        validate_vector([0.1, math.inf, 0.3], 3)


def test_rejects_negative_infinity() -> None:
    with pytest.raises(ProviderError, match="infinity"):
        validate_vector([0.1, -math.inf, 0.3], 3)


def test_reports_the_offending_position() -> None:
    with pytest.raises(ProviderError) as caught:
        validate_vector([0.1, 0.2, math.nan], 3)
    assert caught.value.details.get("position") == 2


# =============================================================================
# normalise / truncate
# =============================================================================
def test_normalise_produces_unit_length() -> None:
    vector = IEmbeddingProvider.normalise([3.0, 4.0])
    assert sum(v * v for v in vector) == pytest.approx(1.0)


def test_normalise_leaves_a_zero_vector_alone() -> None:
    """Dividing by zero would produce NaNs that poison every comparison."""
    assert IEmbeddingProvider.normalise([0.0, 0.0]) == [0.0, 0.0]


def test_truncate_slices_and_renormalises() -> None:
    sliced = IEmbeddingProvider.truncate([1.0, 2.0, 3.0, 4.0], 2)
    assert len(sliced) == 2
    assert sum(v * v for v in sliced) == pytest.approx(1.0)


def test_truncate_is_a_no_op_when_already_short_enough() -> None:
    assert IEmbeddingProvider.truncate([1.0, 2.0], 4) == [1.0, 2.0]


# =============================================================================
# Configuration diagnostics
# =============================================================================
def _findings(settings: Any) -> dict[str, Any]:
    return {finding.check: finding for finding in check_configuration(settings)}


def test_nvidia_without_a_key_is_fatal(settings_env: Any) -> None:
    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM="2048",
        EMBEDDING_STORAGE="halfvec",
        NVIDIA_API_KEY="",
    )
    finding = _findings(settings)["credentials"]
    assert finding.ok is False
    assert finding.fatal is True


def test_nvidia_with_a_key_passes(settings_env: Any) -> None:
    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM="2048",
        EMBEDDING_STORAGE="halfvec",
        NVIDIA_API_KEY="nvapi-x",
    )
    findings = _findings(settings)
    assert findings["credentials"].ok
    assert findings["dimension"].ok
    assert findings["index_capability"].ok


def test_a_non_url_endpoint_is_fatal(settings_env: Any) -> None:
    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        NVIDIA_API_KEY="k",
        NVIDIA_BASE_URL="nim.test/v1",
    )
    finding = _findings(settings)["endpoint"]
    assert finding.ok is False and finding.fatal is True


def test_dimension_above_the_model_width_is_fatal(settings_env: Any) -> None:
    """Asking for more dimensions than the model emits cannot be satisfied, and
    the platform never pads to hide it."""
    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM="3072",
        EMBEDDING_STORAGE="halfvec",
        NVIDIA_API_KEY="k",
    )
    finding = _findings(settings)["dimension"]
    assert finding.ok is False and finding.fatal is True


def test_matryoshka_truncation_is_allowed_and_announced(settings_env: Any) -> None:
    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM="1024",
        EMBEDDING_STORAGE="halfvec",
        NVIDIA_API_KEY="k",
    )
    finding = _findings(settings)["dimension"]
    assert finding.ok is True
    assert "Matryoshka" in finding.detail


def test_2048_on_a_plain_vector_column_is_fatal(settings_env: Any) -> None:
    """The central constraint: pgvector's HNSW index stops at 2000 for `vector`.

    Without this check the column is created, inserts succeed, and every similarity
    search silently becomes a sequential scan.
    """
    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM="2048",
        EMBEDDING_STORAGE="vector",
        NVIDIA_API_KEY="k",
    )
    finding = _findings(settings)["index_capability"]
    assert finding.ok is False
    assert finding.fatal is True
    assert "halfvec" in finding.detail


def test_2048_on_halfvec_is_indexable(settings_env: Any) -> None:
    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM="2048",
        EMBEDDING_STORAGE="halfvec",
        NVIDIA_API_KEY="k",
    )
    assert _findings(settings)["index_capability"].ok


def test_hnsw_limits_match_pgvector() -> None:
    assert HNSW_MAX_DIMS["vector"] == 2000
    assert HNSW_MAX_DIMS["halfvec"] == 4000


# =============================================================================
# Provider resolution
# =============================================================================
def test_nvidia_is_resolvable(settings_env: Any) -> None:
    settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM="2048",
        NVIDIA_API_KEY="k",
        NVIDIA_BASE_URL="https://nim.test/v1",
    )
    from app.ai.embedding import get_embedding_provider

    provider = get_embedding_provider()
    assert provider.name == "nvidia"
    assert provider.model == "nvidia/nemotron-3-embed-1b"
    assert provider.dim == 2048


def test_the_default_model_is_nemotron(settings_env: Any) -> None:
    """No OpenAI embedding model should be reachable by default."""
    settings = settings_env(EMBEDDING_PROVIDER="mock")
    assert settings.embedding.model == "nvidia/nemotron-3-embed-1b"
    assert settings.embedding.dim == 2048


def test_the_default_storage_can_index_the_default_dimension(settings_env: Any) -> None:
    settings = settings_env(EMBEDDING_PROVIDER="mock")
    assert settings.embedding.dim <= HNSW_MAX_DIMS[settings.embedding.storage]
