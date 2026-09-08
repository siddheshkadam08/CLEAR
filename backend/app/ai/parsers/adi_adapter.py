"""Azure Document Intelligence parser adapter (``ACTIVE_PARSER=adi``).

The primary PDF parser. It reuses
:class:`~app.ai.parsers.layout.LayoutParser` for all of the mapping - the
coordinate maths, section planning, table de-duplication and signature
detection - and supplies only the transport.

Two things about that transport are unlike any other parser here, and both are
the reason Azure could not simply be pointed at from an existing adapter:

* **Analysis is a long-running operation.** The submit returns ``202 Accepted``
  with an empty body and an ``Operation-Location`` header; the result is
  collected by polling that URL until its ``status`` leaves ``running``. An
  adapter that simply POSTs and reads the body fails in a uniquely confusing way
  here - the resource *root* answers ``200`` with a zero-length body, which is a
  liveness reply, so the upload looks like it succeeded and the parse fails
  claiming the service returned nothing.
* **The operation path is versioned.** ``2024-11-30`` and later live under
  ``/documentintelligence/``; ``2023-07-31`` lives under ``/formrecognizer/``.
  ``AZURE_DOCINTEL_API_VERSION`` therefore selects the path as well as the
  payload shape.

The whole-document ``analyzeResult`` is divided into the per-page payloads
``LayoutParser._build_page`` expects. That division is not a filter: a table's
cells reference paragraphs by *index*, so the references are rewritten to each
page's own numbering as the split happens - see :func:`split_pages`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from app.ai.parsers.base import ParserCapabilities, ParseRequest
from app.ai.parsers.layout import LayoutParser
from app.core.config import get_settings
from app.core.enums import FileType
from app.core.errors import CorruptedDocumentError, ParserError, ParserTimeoutError
from app.core.logging import get_logger
from app.core.versions import PARSER_FRAMEWORK_VERSION

logger = get_logger(__name__)

#: Element references look like ``/paragraphs/12``; only the index needs rewriting.
_PARAGRAPH_REF = "/paragraphs/"


def split_pages(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """One whole-document ``analyzeResult`` -> one self-contained payload per page.

    The counterpart to :func:`app.ai.parsers.pdfextract_adapter.split_pages`, with
    one addition that matters for Azure: **tables are carried through**, and their
    cell references are renumbered.

    Azure lists a table cell's text twice - inside ``tables[].cells[]`` and again
    as a standalone entry in ``paragraphs[]`` - and ``_build_page`` relies on
    ``cells[].elements`` (``"/paragraphs/12"``) to recognise the duplicate and drop
    the loose copy. Those indices address the *document's* paragraph list. Splitting
    per page renumbers that list, so carrying the tables over untouched would make
    every reference point at whatever paragraph happens to sit at that index on the
    page - de-duplicating the wrong prose and anchoring the table in the wrong
    place. Dropping the tables instead would keep the text but lose every fee
    schedule's structure.

    So each cell's references are translated to the page's own numbering, and
    references to paragraphs on other pages are dropped: a table that spans a page
    break is emitted on the page its first region names, carrying the cells that
    live there.

    A paragraph with no ``boundingRegions`` is dropped rather than guessed at, for
    the reason the pdfextract splitter gives: a confidently wrong page citation is
    worse in this product than a missing paragraph.
    """
    result = analysis.get("analyzeResult", analysis)

    # Document-level paragraph index -> its index within its own page, which is the
    # only numbering `_build_page` ever sees.
    local_of: dict[int, int] = {}
    page_of: dict[int, int] = {}
    paragraphs_by_page: dict[int, list[dict[str, Any]]] = {}
    orphans = 0

    for index, paragraph in enumerate(result.get("paragraphs") or []):
        regions = paragraph.get("boundingRegions") or []
        number = regions[0].get("pageNumber") if regions else None
        if not isinstance(number, int):
            orphans += 1
            continue
        page_paragraphs = paragraphs_by_page.setdefault(number, [])
        local_of[index] = len(page_paragraphs)
        page_of[index] = number
        page_paragraphs.append(paragraph)

    if orphans:
        logger.warning("adi_paragraphs_without_page", count=orphans)

    tables_by_page: dict[int, list[dict[str, Any]]] = {}
    for table in result.get("tables") or []:
        number = _table_page(table, page_of)
        if number is None:
            continue
        tables_by_page.setdefault(number, []).append(
            _renumber_table(table, number, local_of, page_of)
        )

    return [
        {
            # A list of one: `_page_of` reads `pages[0]`, because the page a payload
            # describes is always its own first entry.
            "pages": [page],
            "paragraphs": paragraphs_by_page.get(page.get("pageNumber"), []),
            "tables": tables_by_page.get(page.get("pageNumber"), []),
            "modelId": result.get("modelId", "prebuilt-layout"),
            "apiVersion": result.get("apiVersion"),
        }
        for page in result.get("pages") or []
    ]


def _table_page(table: dict[str, Any], page_of: dict[int, int]) -> int | None:
    """The page a table belongs to: its first region, else its first cell's page."""
    regions = table.get("boundingRegions") or []
    number = regions[0].get("pageNumber") if regions else None
    if isinstance(number, int):
        return number
    # No region of its own - fall back to where its content actually is, so a table
    # is misplaced rather than lost.
    for index in sorted(_referenced_paragraphs(table)):
        if index in page_of:
            return page_of[index]
    return None


def _renumber_table(
    table: dict[str, Any],
    page_number: int,
    local_of: dict[int, int],
    page_of: dict[int, int],
) -> dict[str, Any]:
    """Rewrite a table's cell references from document to page-local numbering."""
    cells: list[dict[str, Any]] = []
    for cell in table.get("cells") or []:
        rewritten: list[str] = []
        for reference in cell.get("elements") or []:
            text = str(reference)
            if not text.startswith(_PARAGRAPH_REF):
                # Figures and other element kinds are passed through untouched;
                # `_cell_paragraph_indices` only reads paragraph references.
                rewritten.append(text)
                continue
            try:
                index = int(text[len(_PARAGRAPH_REF) :])
            except ValueError:
                continue
            if page_of.get(index) != page_number:
                continue
            rewritten.append(f"{_PARAGRAPH_REF}{local_of[index]}")
        cells.append({**cell, "elements": rewritten})
    return {**table, "cells": cells}


def _referenced_paragraphs(table: dict[str, Any]) -> set[int]:
    """Document-level paragraph indices a table's cells reference."""
    indices: set[int] = set()
    for cell in table.get("cells") or []:
        for reference in cell.get("elements") or []:
            text = str(reference)
            if text.startswith(_PARAGRAPH_REF):
                try:
                    indices.add(int(text[len(_PARAGRAPH_REF) :]))
                except ValueError:
                    continue
    return indices


class AzureDocumentIntelligenceParser(LayoutParser):
    """Layout parser backed by Azure Document Intelligence."""

    @property
    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            name="adi",
            version=PARSER_FRAMEWORK_VERSION,
            supported_types=frozenset({FileType.PDF}),
            supports_coordinates=True,
            supports_tables=True,
            supports_sections=True,
            supports_lists=True,
            supports_images=False,
            # Azure OCRs scanned pages as part of layout analysis, so no local pass
            # is needed. It does not report whether it applied any, though.
            supports_ocr=True,
            supports_signatures=True,
            is_remote=True,
        )

    def _fixture_source(self) -> str:
        return get_settings().parser.azure_docintel_endpoint

    # =========================================================================
    # Transport
    # =========================================================================
    def _analyse_url(self) -> str:
        """Where the document is submitted.

        Built from the resource origin rather than taken whole, so the endpoint
        setting stays a resource address and the API version can move the path.
        """
        settings = get_settings().parser
        origin = settings.azure_docintel_endpoint.rstrip("/")
        version = settings.azure_docintel_api_version
        # 2023-07-31 and earlier are Form Recognizer; 2024-11-30 onward are
        # Document Intelligence, at a different path on the same resource.
        segment = "formrecognizer" if version.startswith("2023") else "documentintelligence"
        model = settings.azure_docintel_model
        return f"{origin}/{segment}/documentModels/{model}:analyze?api-version={version}"

    async def _analyse(self, request: ParseRequest) -> list[dict[str, Any]]:
        """Submit the PDF, poll the operation, and split the result per page."""

        settings = get_settings().parser
        if not settings.azure_docintel_endpoint:
            raise ParserError(
                "AZURE_DOCINTEL_ENDPOINT is not configured, so the adi parser cannot run.",
                retryable=False,
            )
        if not settings.azure_docintel_key:
            raise ParserError(
                "AZURE_DOCINTEL_KEY is not configured, so the adi parser cannot run.",
                retryable=False,
            )

        url = self._analyse_url()
        budget = float(settings.azure_docintel_timeout_seconds)
        deadline = time.monotonic() + budget
        headers = {
            # Azure's own header name, fixed by its contract rather than
            # configurable: there is no deployment in which it differs.
            "Ocp-Apim-Subscription-Key": settings.azure_docintel_key,
            "Content-Type": "application/pdf",
        }

        # Both steps open a connection per request rather than sharing a pool. That
        # is unusual and deliberate - see `_submit` for the measurement behind it.
        # The per-request timeout is a ceiling; the overall budget is `deadline`,
        # which has to span the submit and every poll after it.
        operation = await self._submit(url, request, headers=headers, deadline=deadline)
        result = await self._collect(operation, headers=headers, deadline=deadline)

        payloads = split_pages(result)
        if not payloads:
            raise ParserError(
                "Azure Document Intelligence returned an analysis with no pages.",
                retryable=False,
                details={"model": settings.azure_docintel_model},
            )
        logger.info(
            "adi_analysis_complete",
            pages=len(payloads),
            model=settings.azure_docintel_model,
            file_name=request.file_name,
        )
        return payloads

    async def _submit(
        self,
        url: str,
        request: ParseRequest,
        *,
        headers: dict[str, str],
        deadline: float,
    ) -> str:
        """POST the document and return the operation URL to poll.

        Retries the network-policy rejection described in :meth:`_is_vnet_rejection`,
        **on a new connection each time**, which is the whole point of the loop.

        The rejection is decided per connection, from the address the request
        leaves by, and a host behind a multi-WAN link has more than one. Retrying
        over a pooled connection therefore repeats the same verdict forever: this
        was measured against the live resource, where twelve retries on one keep-
        alive connection were refused twelve times, while a fresh client per
        attempt succeeded on the third - the same one-in-three that `curl`, which
        cannot pool across processes, gets by construction.

        So each attempt builds its own client. That is deliberately wasteful of a
        TLS handshake, and it is the difference between the parser working and not.
        Bounded by the analysis deadline, so a resource that rejects *every*
        address fails in one timeout rather than hanging.
        """
        import httpx

        attempt = 0
        while True:
            attempt += 1
            try:
                # A new client, not a new request on an old one - a pooled
                # connection would reuse the egress path that was just refused.
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=15.0),
                    follow_redirects=False,
                ) as client:
                    response = await client.post(url, content=request.content, headers=headers)
            except httpx.TimeoutException as exc:
                raise ParserTimeoutError(
                    "Azure Document Intelligence did not accept the document in time.",
                    details={"endpoint": url},
                ) from exc
            except httpx.HTTPError as exc:
                raise ParserError(
                    f"Could not reach Azure Document Intelligence: {exc}",
                    retryable=True,
                    details={"endpoint": url},
                ) from exc

            if not self._is_vnet_rejection(response):
                break

            if time.monotonic() >= deadline:
                raise ParserError(
                    "Azure Document Intelligence refused every attempt with a virtual "
                    "network restriction. Add this host's public address to the "
                    "resource's firewall - note a host with more than one egress "
                    "address needs all of them allowed.",
                    retryable=True,
                    details={"endpoint": url, "attempts": attempt},
                )
            logger.warning("adi_vnet_rejected_retrying", attempt=attempt, endpoint=url)
            await asyncio.sleep(
                min(
                    float(get_settings().parser.azure_docintel_poll_seconds),
                    max(0.0, deadline - time.monotonic()),
                )
            )

        if response.status_code >= 400:
            self._raise_for_status(response, endpoint=url)

        operation = response.headers.get("operation-location")
        if not operation:
            # A 2xx without the header means the URL was not the analyse operation -
            # the resource root answers exactly this way, which is the failure this
            # adapter exists to stop being mysterious.
            raise ParserError(
                "Azure accepted the request but returned no Operation-Location, so "
                "this endpoint is not the analyse operation. Check "
                "AZURE_DOCINTEL_ENDPOINT is the resource origin and "
                "AZURE_DOCINTEL_API_VERSION matches the resource.",
                retryable=False,
                details={"endpoint": url, "status": response.status_code},
            )
        return str(operation)

    async def _collect(
        self,
        operation: str,
        *,
        headers: dict[str, str],
        deadline: float,
    ) -> dict[str, Any]:
        """Poll the long-running operation until it settles, and return its result."""
        import httpx

        settings = get_settings().parser
        # The poll is a GET; sending the PDF content type on it would be a lie and
        # some gateways reject it.
        poll_headers = {"Ocp-Apim-Subscription-Key": headers["Ocp-Apim-Subscription-Key"]}
        floor = float(settings.azure_docintel_poll_seconds)
        polls = 0

        while True:
            if time.monotonic() >= deadline:
                raise ParserTimeoutError(
                    f"Azure Document Intelligence did not finish within "
                    f"{settings.azure_docintel_timeout_seconds}s.",
                    details={"operation": operation, "polls": polls},
                )

            try:
                # Its own connection, for the reason `_submit` gives: the network
                # policy is applied per connection, and a poll refused on a pooled
                # one would be refused for the rest of the analysis - discarding a
                # result Azure had already computed and charged for.
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=15.0),
                    follow_redirects=False,
                ) as client:
                    response = await client.get(operation, headers=poll_headers)
            except httpx.HTTPError as exc:
                raise ParserError(
                    f"Could not poll the Azure analysis: {exc}",
                    retryable=True,
                    details={"operation": operation},
                ) from exc

            if self._is_vnet_rejection(response):
                # Not a failure of the analysis - just this connection. Poll again.
                logger.warning("adi_vnet_rejected_polling", polls=polls, operation=operation)
                await asyncio.sleep(min(floor, max(0.0, deadline - time.monotonic())))
                continue

            if response.status_code >= 400:
                self._raise_for_status(response, endpoint=operation)

            body = response.json()
            status = str(body.get("status") or "").lower()
            polls += 1

            if status == "succeeded":
                return dict(body)
            if status in {"failed", "canceled", "cancelled"}:
                error = body.get("error") or {}
                code = str(error.get("code") or "unknown")
                message = str(error.get("message") or "Azure reported no reason.")
                # `InvalidContent`/`InvalidImage` are the document's fault and will
                # fail identically on retry; anything else may not.
                if code in {"InvalidContent", "InvalidImage", "InvalidContentDimensions"}:
                    raise CorruptedDocumentError(
                        f"Azure Document Intelligence could not read the document: {message}",
                        details={"code": code},
                    )
                raise ParserError(
                    f"Azure Document Intelligence failed the analysis ({code}): {message}",
                    retryable=True,
                    details={"code": code, "operation": operation},
                )

            # Still running. Azure's `retry-after` is authoritative when present.
            wait = floor
            retry_after = response.headers.get("retry-after")
            if retry_after:
                with contextlib.suppress(ValueError):
                    wait = max(floor, float(retry_after))
            await asyncio.sleep(min(wait, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _is_vnet_rejection(response: Any) -> bool:
        """Is this the resource's network policy refusing *this connection*?

        Azure answers a request from an address outside the resource's virtual
        network or firewall allow-list with ``403`` and a message naming the
        virtual network. It reads like a configuration error and is easy to
        mistake for a bad key, but it is a property of the route the request took,
        not of the credential: measured on this host, the identical request
        alternated between ``403`` and ``202`` because the machine egresses from
        two public addresses and only one of them was allowed.

        Matched on the message rather than the status alone, so a genuinely wrong
        key still fails immediately instead of being retried to the deadline.
        """
        if response.status_code != 403:
            return False
        return "virtual network" in response.text.lower()

    @staticmethod
    def _raise_for_status(response: Any, *, endpoint: str) -> None:
        """Turn an Azure error response into the right ParserError."""
        status = response.status_code
        body = response.text[:500]
        code = ""
        try:
            payload = response.json()
            error = payload.get("error") or {}
            code = str(error.get("code") or "")
            inner = error.get("innererror") or {}
            code = str(inner.get("code") or code)
        except Exception as exc:  # noqa: BLE001 - an unparseable body is still an error
            logger.debug("adi_error_body_unparsed", status=status, error=str(exc)[:120])

        if status == 400 and code in {"InvalidContent", "InvalidImage", "InvalidContentDimensions"}:
            raise CorruptedDocumentError(
                "Azure Document Intelligence rejected the document as unreadable.",
                details={"status": status, "code": code, "body": body},
            )
        if status in {401, 403}:
            raise ParserError(
                "Azure Document Intelligence rejected the credential. Check "
                "AZURE_DOCINTEL_KEY belongs to the resource in AZURE_DOCINTEL_ENDPOINT.",
                # A 403 that names the virtual network is handled before this and
                # never arrives here; anything left is the key itself, which no
                # amount of retrying repairs.
                retryable=False,
                details={"status": status, "endpoint": endpoint},
            )
        if status == 404:
            raise ParserError(
                "Azure Document Intelligence has no such operation. Check "
                "AZURE_DOCINTEL_MODEL and AZURE_DOCINTEL_API_VERSION - the path "
                "differs between API versions.",
                retryable=False,
                details={"status": status, "endpoint": endpoint},
            )
        raise ParserError(
            f"Azure Document Intelligence returned {status}.",
            # 429 and 5xx are the service's problem and may clear; other 4xx will not.
            retryable=status == 429 or status >= 500,
            details={"status": status, "code": code, "body": body},
        )

    async def health(self) -> bool:
        """Is the resource reachable and answering?

        The resource root answers ``200`` with an empty body when it is up, which is
        useless as a contract check but exactly right as a liveness one.
        """
        settings = get_settings().parser
        endpoint = settings.azure_docintel_endpoint
        if not endpoint or not settings.azure_docintel_key:
            return False

        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(endpoint)
            return response.status_code < 500
        except httpx.HTTPError as exc:
            logger.warning("adi_health_failed", error=str(exc))
            return False
