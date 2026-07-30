"""Stage 2 - Parser.

Selects a parser through the registry and produces the
:class:`~app.ai.cdm.models.NormalizedDocument` artifact. The stage itself contains
no parsing logic - that is entirely inside the adapter (§9), which is what makes
switching parsers a configuration change.

The parser name and version are stamped on the checkpoint, so changing
``ACTIVE_PARSER`` invalidates this stage (and everything downstream) for future
runs while leaving already-processed contracts reproducible against the parser that
actually produced them.
"""

from __future__ import annotations

from app.ai.parsers import ParseRequest, get_parser, get_parser_by_name
from app.core.config import get_settings
from app.core.enums import ArtifactKind, PipelineStage
from app.core.errors import ParserError, ParserTimeoutError, StorageError
from app.core.logging import get_logger
from app.core.metrics import ocr_pages_total, pages_processed_total
from app.core.versions import PARSER_ADAPTER_VERSIONS, PARSER_FRAMEWORK_VERSION, ComponentVersions
from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    register_stage,
)

logger = get_logger(__name__)


class ParserStage(StageHandler):
    stage = PipelineStage.PARSER
    requires = (ArtifactKind.VALIDATION,)
    cacheable = True
    retryable = True

    def versions_for(self, ctx: StageContext) -> ComponentVersions:
        """Version by the parser actually used, not the configured default.

        A contract parsed by the PyMuPDF fallback must not appear current when the
        configured parser is Docling - the artifact came from a different engine and
        should be regenerated once Docling is available.
        """
        parser_name = self._resolve_parser_name(ctx)
        return ComponentVersions(
            parser_name=parser_name,
            parser_version=PARSER_FRAMEWORK_VERSION,
            parser_adapter_version=PARSER_ADAPTER_VERSIONS.get(parser_name, "unknown"),
        )

    @staticmethod
    def _resolve_parser_name(ctx: StageContext) -> str:
        """Which adapter will handle this document."""
        pinned = ctx.options.get("parser")
        try:
            parser = (
                get_parser_by_name(str(pinned)) if pinned else get_parser(ctx.contract.file_type)
            )
            return parser.capabilities.name
        except Exception:  # noqa: BLE001 - resolution errors surface in run()
            return get_settings().parser.active_parser

    async def run(self, ctx: StageContext) -> StageResult:
        settings = get_settings()
        contract = ctx.contract

        await ctx.report_progress(5, "loading document")

        try:
            content = await ctx.storage.get_bytes(contract.storage_path)
        except Exception as exc:
            raise StorageError(
                f"Could not read the stored document: {exc}",
                details={"storage_path": contract.storage_path},
            ) from exc

        pinned = ctx.options.get("parser")
        parser = get_parser_by_name(str(pinned)) if pinned else get_parser(contract.file_type)
        capabilities = parser.capabilities

        logger.info(
            "parsing_started",
            contract_id=str(contract.id),
            parser=capabilities.name,
            file_type=contract.file_type.value,
            size_bytes=len(content),
        )

        # OCR is allowed only when enabled, supported by the adapter, and not disabled
        # for this run. A profile can request an OCR-enhanced pass via a workflow
        # extension, which arrives here as an option.
        allow_ocr = (
            settings.parser.ocr_enabled
            and capabilities.supports_ocr
            and bool(ctx.options.get("allow_ocr", True))
        )

        request = ParseRequest(
            document_id=str(contract.id),
            project_id=str(contract.project_id),
            organization_id=str(settings.organization_id),
            file_name=contract.original_file_name,
            storage_path=contract.storage_path,
            file_type=contract.file_type,
            content=content,
            file_hash=contract.sha256_hash,
            allow_ocr=allow_ocr,
            language_hint=contract.language or ctx.options.get("language_hint"),
            options=dict(ctx.options),
        )

        await ctx.report_progress(10, f"parsing with {capabilities.name}")

        try:
            normalized = await self._parse_with_timeout(parser, request, settings)
        except (ParserError, ParserTimeoutError):
            raise
        except Exception as exc:
            raise ParserError(
                f"The {capabilities.name} parser failed: {exc}", stage=self.stage.value
            ) from exc

        quality = parser.quality_from(normalized)

        # A parse that recovered essentially no text is a failure wearing a success
        # badge; fail it so it is retried or routed to a different parser rather than
        # producing a contract with no extractable content.
        if quality.missing_text:
            raise ParserError(
                "The parser recovered almost no text from this document. It may be a "
                "scanned image without OCR available, or malformed.",
                stage=self.stage.value,
                details={
                    "parser": capabilities.name,
                    "pages": len(normalized.pages),
                    "paragraphs": len(normalized.paragraphs),
                    "ocr_attempted": allow_ocr,
                },
            )

        # Attach the recomputed quality so enrichment starts from measured values.
        normalized = normalized.model_copy(update={"quality": quality})

        page_count = len(normalized.pages)
        ocr_page_count = len(quality.scanned_pages)

        pages_processed_total.labels(parser=capabilities.name).inc(page_count)
        if ocr_page_count:
            ocr_pages_total.labels(engine=settings.parser.ocr_engine).inc(ocr_page_count)

        await ctx.report_progress(32, "parsed")

        logger.info(
            "parsing_completed",
            contract_id=str(contract.id),
            parser=capabilities.name,
            pages=page_count,
            sections=len(normalized.sections),
            paragraphs=len(normalized.paragraphs),
            tables=len(normalized.tables),
            ocr_pages=ocr_page_count,
            coordinate_coverage=quality.coordinate_coverage,
        )

        return StageResult(
            artifacts=[
                StageArtifact(
                    kind=ArtifactKind.NORMALIZED_DOCUMENT,
                    payload=normalized.model_dump(mode="json"),
                    summary={
                        "parser": capabilities.name,
                        "pages": page_count,
                        "sections": len(normalized.sections),
                        "paragraphs": len(normalized.paragraphs),
                        "tables": len(normalized.tables),
                        "lists": len(normalized.lists),
                        "ocr_pages": ocr_page_count,
                        "coordinate_coverage": quality.coordinate_coverage,
                    },
                )
            ],
            stats={
                "parser_name": capabilities.name,
                "pages_parsed": page_count,
                "ocr_pages": ocr_page_count,
                "paragraphs": len(normalized.paragraphs),
                "tables": len(normalized.tables),
            },
            context_updates={
                "page_count": page_count,
                "language": normalized.metadata.language,
                "parser_name": capabilities.name,
            },
            warnings=quality.warnings,
        )

    @staticmethod
    async def _parse_with_timeout(parser, request, settings):  # type: ignore[no-untyped-def]
        """Bound the parse.

        A hung parser would otherwise hold a worker slot indefinitely and stall the
        pool; the timeout converts that into a retryable failure.
        """
        import asyncio

        try:
            return await asyncio.wait_for(
                parser.parse(request), timeout=settings.parser.timeout_seconds
            )
        except TimeoutError as exc:
            raise ParserTimeoutError(
                f"Parsing exceeded {settings.parser.timeout_seconds}s and was abandoned.",
                stage=PipelineStage.PARSER.value,
                details={"parser": parser.capabilities.name},
            ) from exc


register_stage(ParserStage())

__all__ = ["ParserStage"]
