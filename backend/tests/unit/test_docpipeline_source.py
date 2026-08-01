"""Loading page JSON off disk: ordering, geometry, and the fallbacks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.docpipeline.source import load_pages, union_polygon


def _write(directory: Path, name: str, payload: dict) -> None:
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _page(number: int, paragraphs: list[dict]) -> dict:
    return {
        "pages": [{"pageNumber": number, "width": 8.5, "height": 11.0, "unit": "inch"}],
        "paragraphs": paragraphs,
    }


def _para(content: str, *, role: str | None = None, polygon: list[float] | None = None) -> dict:
    entry: dict = {"content": content}
    if role:
        entry["role"] = role
    entry["boundingRegions"] = [
        {"pageNumber": 1, "polygon": polygon or [1.0, 1.0, 2.0, 1.0, 2.0, 2.0, 1.0, 2.0]}
    ]
    return entry


def test_pages_sort_numerically_not_lexically(tmp_path: Path) -> None:
    """page_10 comes after page_2.

    Lexical ordering puts page_10 between page_1 and page_2 and silently
    reorders the document into something that still reads plausibly.
    """
    for number in (1, 2, 10):
        _write(tmp_path, f"page_{number}.json", _page(number, [_para(f"body of page {number}")]))

    pages = load_pages(tmp_path)

    assert [page.page_number for page in pages] == [1, 2, 10]


def test_paragraph_order_content_and_roles_survive(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "page_1.json",
        _page(
            1,
            [
                _para("6. INSPECTION & AUDIT", role="sectionHeading"),
                _para("6.1 The Sourcing Partner agrees ..."),
                _para("   "),  # whitespace only - dropped
                _para("6.2 The Sourcing Partner shall disclose ..."),
            ],
        ),
    )

    (page,) = load_pages(tmp_path)

    assert [p.content for p in page.paragraphs] == [
        "6. INSPECTION & AUDIT",
        "6.1 The Sourcing Partner agrees ...",
        "6.2 The Sourcing Partner shall disclose ...",
    ]
    assert page.paragraphs[0].is_heading
    assert not page.paragraphs[1].is_heading
    # Refs are page.position and renumber after the blank is dropped.
    assert [p.ref for p in page.paragraphs] == ["1.1", "1.2", "1.3"]


def test_polygon_is_read_as_floats(tmp_path: Path) -> None:
    """Coordinates are inches and must stay fractional.

    Truncating to integers collapses every box on the page to the same few
    coordinates.
    """
    polygon = [1.7371, 1.0047, 7.4946, 0.9993, 7.4950, 1.4410, 1.7375, 1.4463]
    _write(tmp_path, "page_9.json", _page(9, [_para("complaint, case, dispute", polygon=polygon)]))

    (page,) = load_pages(tmp_path)

    assert page.paragraphs[0].polygon == tuple(polygon)


def test_page_number_falls_back_to_filename(tmp_path: Path) -> None:
    """A page with an empty `pages` array still belongs somewhere."""
    _write(tmp_path, "page_7.json", {"pages": [], "paragraphs": [_para("orphan page")]})

    (page,) = load_pages(tmp_path)

    assert page.page_number == 7
    assert page.paragraphs[0].ref == "7.1"


def test_paragraph_without_geometry_is_kept(tmp_path: Path) -> None:
    """Missing geometry is not a reason to discard real text."""
    _write(tmp_path, "page_1.json", {"pages": [], "paragraphs": [{"content": "no polygon here"}]})

    (page,) = load_pages(tmp_path)

    assert page.paragraphs[0].content == "no polygon here"
    assert page.paragraphs[0].polygon == ()


def test_empty_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No page_"):
        load_pages(tmp_path)


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Not a directory"):
        load_pages(tmp_path / "nope")


def test_union_polygon_covers_every_vertex() -> None:
    """The union box has to contain both inputs, including a skewed one."""
    first = (1.0, 1.0, 3.0, 1.0, 3.0, 2.0, 1.0, 2.0)
    second = (2.0, 5.0, 6.0, 4.9, 6.0, 6.0, 2.0, 6.1)

    box = union_polygon([first, second])

    xs, ys = box[0::2], box[1::2]
    assert min(xs) == 1.0
    assert max(xs) == 6.0
    assert min(ys) == 1.0
    assert max(ys) == 6.1


def test_union_polygon_ignores_unusable_input() -> None:
    assert union_polygon([]) == []
    assert union_polygon([(1.0, 2.0)]) == []
