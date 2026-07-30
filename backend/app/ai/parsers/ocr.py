"""OCR helpers for scanned pages.

Invoked by a parser adapter when a page's text layer is effectively empty. Two
engines are supported behind one function so the adapter does not care which is
configured: Tesseract (local, default) and Azure Document Intelligence's read model
(remote).

Word-level boxes are requested rather than plain text because the platform's
evidence promise requires coordinates for OCR-recovered text too - otherwise a
scanned contract produces clauses that cannot be highlighted.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.core.errors import OcrError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Words below this confidence are dropped - OCR noise creates phantom clause text.
_MIN_WORD_CONFIDENCE = 40.0


@dataclass(slots=True)
class OcrLine:
    """One recovered text line with pixel coordinates."""

    text: str
    left: float
    top: float
    width: float
    height: float
    confidence: float | None = None


def ocr_page_image(image_bytes: bytes) -> tuple[list[OcrLine], float | None]:
    """Recover text from a rendered page image.

    Returns ``(lines, mean_confidence)``. Never raises for a recognition failure -
    an OCR miss degrades the page rather than failing the document, and the empty
    result is reported through the CDM's quality metrics.
    """
    settings = get_settings()
    engine = settings.parser.ocr_engine

    try:
        if engine == "tesseract":
            return _tesseract(image_bytes)
        if engine == "azure":
            return _azure(image_bytes)
    except OcrError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("ocr_failed", engine=engine, error=str(exc))
        return [], None

    logger.warning("unknown_ocr_engine", engine=engine)
    return [], None


def _tesseract(image_bytes: bytes) -> tuple[list[OcrLine], float | None]:
    """Tesseract via pytesseract, grouping words into lines."""
    import io

    try:
        import pytesseract
        from PIL import Image as PILImage
    except ImportError as exc:
        raise OcrError(
            "OCR requires the 'parsers' extra: pip install '.[parsers]'", stage="parser"
        ) from exc

    settings = get_settings()
    if settings.parser.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.parser.tesseract_cmd

    image = PILImage.open(io.BytesIO(image_bytes))
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, config="--psm 3")

    # Group words into lines by Tesseract's own block/paragraph/line indices, which
    # is more reliable than clustering by y-coordinate on a skewed scan.
    grouped: dict[tuple[int, int, int], list[int]] = {}
    for index, text in enumerate(data.get("text", [])):
        if not str(text).strip():
            continue
        confidence = float(data.get("conf", [])[index] or -1)
        if confidence < _MIN_WORD_CONFIDENCE:
            continue
        key = (
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        grouped.setdefault(key, []).append(index)

    lines: list[OcrLine] = []
    confidences: list[float] = []

    for indices in grouped.values():
        words = [str(data["text"][i]).strip() for i in indices]
        left = min(float(data["left"][i]) for i in indices)
        top = min(float(data["top"][i]) for i in indices)
        right = max(float(data["left"][i]) + float(data["width"][i]) for i in indices)
        bottom = max(float(data["top"][i]) + float(data["height"][i]) for i in indices)
        word_confidences = [float(data["conf"][i]) for i in indices]
        mean_confidence = sum(word_confidences) / len(word_confidences)
        confidences.extend(word_confidences)

        lines.append(
            OcrLine(
                text=" ".join(words),
                left=left,
                top=top,
                width=right - left,
                height=bottom - top,
                confidence=round(mean_confidence / 100.0, 4),
            )
        )

    lines.sort(key=lambda line: (round(line.top), line.left))
    overall = round(sum(confidences) / len(confidences) / 100.0, 4) if confidences else None
    return lines, overall


def _azure(image_bytes: bytes) -> tuple[list[OcrLine], float | None]:
    """Azure Document Intelligence read model.

    Called synchronously because it runs inside the parser's worker thread; the
    adapter already isolated it from the event loop.
    """
    settings = get_settings()
    if not settings.parser.azure_docintel_endpoint or not settings.parser.azure_docintel_key:
        raise OcrError(
            "Azure OCR requires AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY.",
            stage="parser",
        )

    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential
    except ImportError as exc:
        raise OcrError(
            "Azure OCR requires the 'azure' extra: pip install '.[azure]'", stage="parser"
        ) from exc

    client = DocumentIntelligenceClient(
        endpoint=settings.parser.azure_docintel_endpoint,
        credential=AzureKeyCredential(settings.parser.azure_docintel_key),
    )
    poller = client.begin_analyze_document("prebuilt-read", body=image_bytes)
    result = poller.result()

    lines: list[OcrLine] = []
    confidences: list[float] = []

    for page in getattr(result, "pages", []) or []:
        for line in getattr(page, "lines", []) or []:
            polygon = list(getattr(line, "polygon", []) or [])
            if len(polygon) >= 8:
                xs = polygon[0::2]
                ys = polygon[1::2]
                left, top = min(xs), min(ys)
                width, height = max(xs) - left, max(ys) - top
            else:
                left = top = width = height = 0.0
            lines.append(
                OcrLine(
                    text=str(line.content),
                    left=float(left),
                    top=float(top),
                    width=float(width),
                    height=float(height),
                )
            )
        for word in getattr(page, "words", []) or []:
            if getattr(word, "confidence", None) is not None:
                confidences.append(float(word.confidence))

    overall = round(sum(confidences) / len(confidences), 4) if confidences else None
    return lines, overall


def ocr_available() -> bool:
    """Is the configured OCR engine usable?"""
    settings = get_settings()
    if not settings.parser.ocr_enabled:
        return False
    if settings.parser.ocr_engine == "tesseract":
        try:
            import pytesseract

            if settings.parser.tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = settings.parser.tesseract_cmd
            pytesseract.get_tesseract_version()
            return True
        except Exception:  # noqa: BLE001
            return False
    return bool(settings.parser.azure_docintel_endpoint and settings.parser.azure_docintel_key)


__all__ = ["OcrLine", "ocr_available", "ocr_page_image"]
