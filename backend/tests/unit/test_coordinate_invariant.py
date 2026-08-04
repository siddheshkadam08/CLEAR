"""Page dimensions must travel with every bounding box, from every parser.

The viewer scales a highlight by ``x / page_width`` and returns ``null`` rather
than guessing when that is absent. So a box without page dimensions is not a
slightly worse highlight - it is no highlight at all, drawn from data that looks
completely valid at every layer in between:

* ``Coordinates.page_width`` is optional, so it constructs fine;
* ``to_dict()`` uses ``exclude_none=True``, so the key does not even survive as a
  null anyone might notice;
* ``BoundingBox`` also has it optional, so the API validates and serves it;
* only ``highlightStyle`` in the browser knows, and it fails silently.

`PyMuPdfParser._coords_from_line` omitted them for every paragraph, list item,
heading and signature - and pymupdf is the fallback the registry picks whenever
the configured parser is unavailable, i.e. exactly the local and demo runs. The
user-visible symptom was precise: the Evidence button appears, the pane switches,
the viewer jumps to the right page, and nothing is highlighted.

``test_extraction_chunks.py`` already asserted this invariant, but only for the
docpipeline path. These apply it at the source, per adapter, so it cannot be true
of one parser and false of another.
"""

from __future__ import annotations

from app.ai.cdm.models import Coordinates
from app.ai.parsers.pymupdf_adapter import PyMuPdfParser, _Line
from app.schemas.common import BoundingBox

PAGE_WIDTH = 612.0
PAGE_HEIGHT = 792.0


def _line(**overrides: object) -> _Line:
    base = {
        "text": "The Supplier shall maintain insurance.",
        "size": 11.0,
        "bold": False,
        "bbox": (72.0, 96.0, 540.0, 112.0),
        "page": 4,
        "order": 7,
        "page_width": PAGE_WIDTH,
        "page_height": PAGE_HEIGHT,
    }
    return _Line(**{**base, **overrides})  # type: ignore[arg-type]


class TestPyMuPdfCoordinates:
    def test_a_line_box_carries_its_page_size(self) -> None:
        box = PyMuPdfParser()._coords_from_line(_line())

        assert box is not None
        assert box.page_width == PAGE_WIDTH
        assert box.page_height == PAGE_HEIGHT

    def test_the_serialised_box_still_carries_it(self) -> None:
        """`exclude_none=True` is why a missing value vanished rather than showing."""
        box = PyMuPdfParser()._coords_from_line(_line())
        assert box is not None

        raw = box.to_dict()

        assert "page_width" in raw, "the key must survive serialisation"
        assert "page_height" in raw

    def test_the_api_schema_accepts_it_and_it_is_drawable(self) -> None:
        """Exactly what `knowledge._boxes()` does, then what the viewer needs."""
        box = PyMuPdfParser()._coords_from_line(_line())
        assert box is not None

        parsed = BoundingBox(**box.to_dict())

        assert parsed.page_width and parsed.page_height
        assert parsed.width > 0 and parsed.height > 0
        # The viewer's own arithmetic. Zero page_width would be a division by zero;
        # None would be the silent no-draw this test exists to prevent.
        assert 0 <= parsed.x / parsed.page_width <= 1


class TestMergePreservesPageSize:
    def test_merging_boxes_on_a_page_keeps_the_dimensions(self) -> None:
        """Chunking merges per page; the merge must not drop what it merges."""
        first = Coordinates(
            page_number=2,
            x=72.0,
            y=100.0,
            width=200.0,
            height=12.0,
            page_width=PAGE_WIDTH,
            page_height=PAGE_HEIGHT,
        )
        second = Coordinates(
            page_number=2,
            x=72.0,
            y=120.0,
            width=300.0,
            height=12.0,
            page_width=PAGE_WIDTH,
            page_height=PAGE_HEIGHT,
        )

        merged = Coordinates.merge([first, second])

        assert merged is not None
        assert merged.page_width == PAGE_WIDTH
        assert merged.page_height == PAGE_HEIGHT
        assert merged.height >= 32.0  # spans both lines
