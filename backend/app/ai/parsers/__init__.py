"""Parser adapter framework (§9).

The platform depends on :class:`~app.ai.parsers.base.IDocumentParser`, never on a
parser. Adapters translate a vendor's output into a
:class:`~app.ai.cdm.models.NormalizedDocument` and nothing vendor-specific escapes
them, so switching parsers is a configuration change plus one adapter.

Adapters resolved by :mod:`app.ai.parsers.registry`:

==============  =====================================  ====================
``adi``         Azure Document Intelligence (default)  layout + coordinates
``pdfextract``  local extractor container or checkout   layout + coordinates
``mock``        deterministic fixture                  tests / CI / no deps
``pymupdf``     local PDF text layer + OCR fallback    always available
``docx``        python-docx                            DOCX
==============  =====================================  ====================

``adi`` and ``pdfextract`` produce the identical ``prebuilt-layout`` payload, so
the registry falls from the first to the second without anything downstream
noticing - the difference is a remote dependency, not fidelity.
"""

from app.ai.parsers.base import IDocumentParser, ParserCapabilities, ParseRequest
from app.ai.parsers.registry import (
    available_parsers,
    get_parser,
    get_parser_by_name,
    parser_health,
    reset_registry,
)

__all__ = [
    "IDocumentParser",
    "ParseRequest",
    "ParserCapabilities",
    "available_parsers",
    "get_parser",
    "get_parser_by_name",
    "parser_health",
    "reset_registry",
]
