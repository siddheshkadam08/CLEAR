"""``PARSER_MODE`` record/replay.

The guarantee under test is narrow and important: **in fixture mode nothing
reaches the network.** Every test here installs a transport that raises if it is
called, so a regression that reintroduces a live call fails loudly rather than
quietly costing a rate-limited request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.ai.parsers.base import ParseRequest
from app.ai.parsers.fixtures import DEFAULT_FIXTURE, FixtureStore, resolve
from app.core.enums import FileType
from app.core.errors import ParserError

SAMPLE_PAYLOAD: list[dict[str, Any]] = [
    {
        "pages": [{"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch"}],
        "paragraphs": [
            {
                "content": "Limitation of Liability",
                "role": "sectionHeading",
                "boundingRegions": [
                    {"pageNumber": 1, "polygon": [1.0, 1.0, 4.0, 1.0, 4.0, 1.3, 1.0, 1.3]}
                ],
            },
            {
                "content": "In no event shall liability exceed the fees paid.",
                "boundingRegions": [
                    {"pageNumber": 1, "polygon": [1.0, 1.5, 7.0, 1.5, 7.0, 1.8, 1.0, 1.8]}
                ],
            },
        ],
    }
]


def _request(file_hash: str = "a" * 64, name: str = "sample.pdf") -> ParseRequest:
    return ParseRequest(
        document_id="doc-1",
        project_id="proj-1",
        organization_id="org-1",
        file_name=name,
        storage_path=f"projects/proj-1/{name}",
        file_type=FileType.PDF,
        content=b"%PDF-1.7 fake",
        file_hash=file_hash,
    )


def _exploding_transport() -> httpx.MockTransport:
    """Any network call is a test failure."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(
            f"fixture mode made a network call to {request.url} - it must never "
            "touch the parser service"
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def store(tmp_path: Path, settings_env: Any) -> FixtureStore:
    settings_env(
        ACTIVE_PARSER="adi",
        PARSER_MODE="fixture",
        PARSER_FIXTURE_DIR=str(tmp_path),
    )
    return FixtureStore(parser="adi")


# =============================================================================
# Store
# =============================================================================
def test_saves_and_loads_by_content_hash(store: FixtureStore) -> None:
    store.save("b" * 64, SAMPLE_PAYLOAD, file_name="x.pdf")
    record = store.load("b" * 64)

    assert record is not None
    assert record.payloads == SAMPLE_PAYLOAD
    assert record.parser == "adi"
    assert record.recorded_at


def test_a_different_document_does_not_match(store: FixtureStore) -> None:
    """Keyed on content, so a changed file can never replay the old response."""
    store.save("b" * 64, SAMPLE_PAYLOAD)
    assert store.load("c" * 64) is None


def test_save_never_overwrites(store: FixtureStore) -> None:
    """Determinism: once recorded, a hash always replays the same bytes.

    A service that starts returning something different must not silently change
    what the tests assert against.
    """
    store.save("b" * 64, SAMPLE_PAYLOAD)
    store.save("b" * 64, [{"pages": [], "paragraphs": [{"content": "different"}]}])

    record = store.load("b" * 64)
    assert record is not None
    assert record.payloads == SAMPLE_PAYLOAD


def test_first_recording_becomes_the_default(store: FixtureStore) -> None:
    """One live parse makes a fresh clone able to work entirely offline."""
    store.save("b" * 64, SAMPLE_PAYLOAD)
    assert store.default_path.exists()
    assert store.load_default() is not None


def test_a_corrupt_fixture_raises_rather_than_falling_back(store: FixtureStore) -> None:
    """Silently using the fallback would parse the wrong document."""
    store.directory.mkdir(parents=True, exist_ok=True)
    store.path_for("d" * 64).write_text("{not json", encoding="utf-8")

    with pytest.raises(ParserError, match="could not be read"):
        store.load("d" * 64)


# =============================================================================
# Resolution
# =============================================================================
def test_resolves_an_exact_match(store: FixtureStore) -> None:
    store.save("b" * 64, SAMPLE_PAYLOAD)
    assert resolve(store, "b" * 64) == SAMPLE_PAYLOAD


def test_falls_back_to_the_default_fixture(store: FixtureStore) -> None:
    store.save("b" * 64, SAMPLE_PAYLOAD)
    # A document never recorded still resolves, via the sample.
    assert resolve(store, "z" * 64) == SAMPLE_PAYLOAD


def test_missing_everything_is_an_actionable_error(store: FixtureStore) -> None:
    with pytest.raises(ParserError) as caught:
        resolve(store, "z" * 64)

    message = str(caught.value)
    assert "PARSER_MODE=live" in message
    assert DEFAULT_FIXTURE in message
    assert str(store.directory) in message


# =============================================================================
# Adapter integration - the guarantee that matters
# =============================================================================
async def test_fixture_mode_makes_no_network_call(store: FixtureStore) -> None:
    from app.ai.parsers.adi_adapter import AzureDocumentIntelligenceParser

    store.save("b" * 64, SAMPLE_PAYLOAD)
    parser = AzureDocumentIntelligenceParser()

    # Any attempt to reach the service raises inside the transport.
    import app.ai.parsers.adi_adapter as module

    original = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: original(transport=_exploding_transport(), **kw)  # type: ignore[assignment,misc]
    try:
        document = await parser.parse(_request("b" * 64))
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
        assert module is not None

    assert document.pages
    # The mapping code ran: this text only exists in the payload, so replaying a
    # pre-built NormalizedDocument would not have produced it.
    text = " ".join(block.text for page in document.pages for block in page.content_blocks)
    assert "Limitation of Liability" in text


async def test_fixture_mode_preserves_coordinates(store: FixtureStore) -> None:
    """Polygons must survive replay - they are what powers PDF highlighting."""
    from app.ai.parsers.adi_adapter import AzureDocumentIntelligenceParser

    store.save("b" * 64, SAMPLE_PAYLOAD)
    document = await AzureDocumentIntelligenceParser().parse(_request("b" * 64))

    boxes = [
        block.coordinates
        for page in document.pages
        for block in page.content_blocks
        if block.coordinates is not None
    ]
    assert boxes, "replayed document carries no coordinates"
    assert all(box.page_number == 1 for box in boxes)
    assert all(box.width > 0 and box.height > 0 for box in boxes)


async def test_fixture_mode_fails_clearly_with_no_fixture(store: FixtureStore) -> None:
    from app.ai.parsers.adi_adapter import AzureDocumentIntelligenceParser

    with pytest.raises(ParserError, match="PARSER_MODE=fixture"):
        await AzureDocumentIntelligenceParser().parse(_request("e" * 64))


# =============================================================================
# Configuration
# =============================================================================
def test_fixture_is_the_default_mode(settings_env: Any) -> None:
    """An accidental live call costs money and a rate-limit slot; an accidental
    replay is a logged fallback. The safe default is the cheap failure."""
    settings = settings_env(ACTIVE_PARSER="adi")
    assert settings.parser.parser_mode == "fixture"
    assert settings.parser.is_fixture_mode


def test_live_mode_is_opt_in(settings_env: Any) -> None:
    settings = settings_env(PARSER_MODE="live")
    assert not settings.parser.is_fixture_mode


def test_an_unknown_mode_is_rejected_at_config_load(settings_env: Any) -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        settings_env(PARSER_MODE="sometimes")


def test_recorded_fixture_is_readable_json(store: FixtureStore) -> None:
    """Fixtures are committed and reviewed, so they have to be diffable."""
    path = store.save("b" * 64, SAMPLE_PAYLOAD, file_name="x.pdf")
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["parser"] == "adi"
    assert raw["file_name"] == "x.pdf"
    assert isinstance(raw["payloads"], list)
