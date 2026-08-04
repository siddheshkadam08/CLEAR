"""Exception hierarchy and the single error envelope every endpoint returns.

Contract (§19): ``{"error": {"code", "message", "details", "trace_id"}}``.

Rules:
  * Business code raises a subclass of :class:`AppError` - never an
    ``HTTPException``. Transport concerns stay in the handlers below.
  * ``message`` is safe to show a user. Anything sensitive goes in the log, not
    the response.
  * Internal failures never leak a stack trace or driver message to the client;
    the ``trace_id`` is the bridge between what the user sees and what the
    operator greps for.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, get_trace_id

logger = get_logger(__name__)


class ErrorCode:
    """Stable, machine-readable error codes. The frontend switches on these."""

    # 400 family
    VALIDATION_ERROR = "validation_error"
    BAD_REQUEST = "bad_request"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    FILE_TOO_LARGE = "file_too_large"
    DUPLICATE_DOCUMENT = "duplicate_document"
    #: A Word document was stored but could not be turned into a PDF. Separate
    #: from a processing failure: the pipeline never started, and the remedy is a
    #: different source file rather than a retry.
    CONVERSION_FAILED = "conversion_failed"
    #: The archive itself could not be read, so nothing inside it was processed.
    ARCHIVE_ERROR = "archive_error"
    INVALID_STATE_TRANSITION = "invalid_state_transition"

    # 401 / 403
    UNAUTHENTICATED = "unauthenticated"
    INVALID_CREDENTIALS = "invalid_credentials"
    TOKEN_EXPIRED = "token_expired"  # noqa: S105 - an error code
    TOKEN_INVALID = "token_invalid"  # noqa: S105 - an error code
    FORBIDDEN = "forbidden"
    PROJECT_ACCESS_DENIED = "project_access_denied"
    PERMISSION_DENIED = "permission_denied"

    # 404 / 409
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"

    # 422
    UNPROCESSABLE = "unprocessable_entity"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"

    # 429
    RATE_LIMITED = "rate_limited"

    # 5xx
    INTERNAL_ERROR = "internal_error"
    DATABASE_ERROR = "database_error"
    STORAGE_ERROR = "storage_error"
    QUEUE_ERROR = "queue_error"
    PARSER_ERROR = "parser_error"
    PIPELINE_ERROR = "pipeline_error"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NOT_IMPLEMENTED = "not_implemented"
    RETRIEVAL_ERROR = "retrieval_error"
    GROUNDING_FAILED = "grounding_failed"


# =============================================================================
# Base
# =============================================================================
class AppError(Exception):
    """Base class for every deliberate application failure."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = ErrorCode.INTERNAL_ERROR
    message: str = "An unexpected error occurred."
    #: Transient failures are safe for the queue to retry.
    retryable: bool = False

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable
        super().__init__(self.message)

    def to_envelope(self, trace_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "trace_id": trace_id or get_trace_id(),
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# =============================================================================
# 4xx
# =============================================================================
class ValidationError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = ErrorCode.VALIDATION_ERROR
    message = "The request payload is invalid."


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = ErrorCode.BAD_REQUEST
    message = "Bad request."


class UnauthenticatedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.UNAUTHENTICATED
    message = "Authentication required."


class InvalidCredentialsError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.INVALID_CREDENTIALS
    # Deliberately does not distinguish unknown email from wrong password.
    message = "Incorrect email or password."


class TokenExpiredError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.TOKEN_EXPIRED
    message = "Your session has expired. Please sign in again."


class TokenInvalidError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.TOKEN_INVALID
    message = "Invalid authentication token."


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.FORBIDDEN
    message = "You do not have permission to perform this action."


class ProjectAccessDeniedError(ForbiddenError):
    """Raised when a user touches a project they are not a member of.

    Project isolation is the platform's security boundary (§1.1), so this is
    logged at WARNING with the user and project id every time.
    """

    code = ErrorCode.PROJECT_ACCESS_DENIED
    message = "You do not have access to this project."


class PermissionDeniedError(ForbiddenError):
    code = ErrorCode.PERMISSION_DENIED
    message = "Your role does not grant this permission."

    def __init__(self, permission: str | None = None, **kwargs: Any) -> None:
        details = kwargs.pop("details", {}) or {}
        if permission:
            details["required_permission"] = permission
        super().__init__(details=details, **kwargs)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.NOT_FOUND
    message = "The requested resource was not found."

    def __init__(
        self,
        resource: str | None = None,
        identifier: Any = None,
        **kwargs: Any,
    ) -> None:
        if resource and "message" not in kwargs:
            kwargs["message"] = f"{resource} not found."
        details = kwargs.pop("details", {}) or {}
        if resource:
            details["resource"] = resource
        if identifier is not None:
            details["id"] = str(identifier)
        super().__init__(details=details, **kwargs)


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.CONFLICT
    message = "The request conflicts with the current state of the resource."


class DuplicateDocumentError(ConflictError):
    """Same SHA-256 already uploaded to this project (§7.2 unique constraint)."""

    code = ErrorCode.DUPLICATE_DOCUMENT
    message = "This document has already been uploaded to this project."


class UnsupportedFileTypeError(ValidationError):
    code = ErrorCode.UNSUPPORTED_FILE_TYPE
    message = "Only PDF, DOC, DOCX and ZIP files are supported."


class ConversionError(ValidationError):
    """A Word document was stored but could not be turned into a PDF.

    Not a ``PipelineError``: the pipeline never started. The original is safe in
    storage and downloadable; what is missing is the PDF everything downstream
    needs. Retrying the same bytes will fail the same way unless the failure was
    a timeout, hence ``retryable`` being set per-raise rather than on the class.
    """

    code = ErrorCode.CONVERSION_FAILED
    message = "This document could not be converted to PDF."


class ArchiveError(ValidationError):
    """The archive could not be read, so nothing inside it was processed.

    Distinct from a member failing: this rejects the whole upload, because an
    archive that will not open has no members to succeed.
    """

    code = ErrorCode.ARCHIVE_ERROR
    message = "This archive could not be opened."


class FileTooLargeError(ValidationError):
    # Numeric literal rather than a Starlette constant: the constant was renamed
    # (REQUEST_ENTITY_TOO_LARGE -> CONTENT_TOO_LARGE) and the number is stable.
    status_code = 413
    code = ErrorCode.FILE_TOO_LARGE
    message = "The uploaded file exceeds the maximum allowed size."


class InvalidStateTransitionError(ConflictError):
    code = ErrorCode.INVALID_STATE_TRANSITION
    message = "That operation is not valid for the current job state."


class UnprocessableError(AppError):
    status_code = 422
    code = ErrorCode.UNPROCESSABLE
    message = "The request could not be processed."


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = ErrorCode.RATE_LIMITED
    message = "Too many requests. Please slow down."

    def __init__(self, retry_after: int | None = None, **kwargs: Any) -> None:
        details = kwargs.pop("details", {}) or {}
        if retry_after is not None:
            details["retry_after_seconds"] = retry_after
        self.retry_after = retry_after
        super().__init__(details=details, **kwargs)


# =============================================================================
# 5xx / infrastructure
# =============================================================================
class DatabaseError(AppError):
    code = ErrorCode.DATABASE_ERROR
    message = "A database error occurred."
    retryable = True


class StorageError(AppError):
    code = ErrorCode.STORAGE_ERROR
    message = "An object storage error occurred."
    retryable = True


class QueueError(AppError):
    code = ErrorCode.QUEUE_ERROR
    message = "Failed to enqueue the job."
    retryable = True


class NotImplementedFeatureError(AppError):
    status_code = status.HTTP_501_NOT_IMPLEMENTED
    code = ErrorCode.NOT_IMPLEMENTED
    message = "This capability is not enabled in this deployment."


# --- pipeline ----------------------------------------------------------------
class PipelineError(AppError):
    """Base for stage failures. Carries the stage so the job row can record it."""

    code = ErrorCode.PIPELINE_ERROR
    message = "A processing stage failed."

    def __init__(self, message: str | None = None, *, stage: str | None = None, **kwargs: Any):
        details = kwargs.pop("details", {}) or {}
        if stage:
            details["stage"] = stage
        self.stage = stage
        super().__init__(message, details=details, **kwargs)


class ParserError(PipelineError):
    code = ErrorCode.PARSER_ERROR
    message = "The document could not be parsed."
    retryable = True


class CorruptedDocumentError(ParserError):
    message = "The document appears to be corrupted or unreadable."
    retryable = False  # retrying a corrupt file wastes a worker slot


class OcrError(ParserError):
    message = "OCR failed for this document."
    retryable = True


class ParserTimeoutError(ParserError):
    message = "Parsing timed out."
    retryable = True


class ChunkingError(PipelineError):
    message = "Semantic chunking failed."


class ExtractionError(PipelineError):
    message = "AI extraction failed."
    retryable = True


class SchemaValidationError(PipelineError):
    """The LLM returned JSON that does not satisfy the extraction schema.

    Retryable: §13 requires invalid output to be rejected *and retried*.
    """

    code = ErrorCode.SCHEMA_VALIDATION_FAILED
    message = "The model response did not match the required schema."
    retryable = True


class EmbeddingError(PipelineError):
    message = "Embedding generation failed."
    retryable = True


class IndexingError(PipelineError):
    message = "Index construction failed."
    retryable = True


# --- AI providers ------------------------------------------------------------
class ProviderError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = ErrorCode.PROVIDER_ERROR
    message = "An upstream AI provider returned an error."
    retryable = True

    def __init__(self, message: str | None = None, *, provider: str | None = None, **kwargs: Any):
        details = kwargs.pop("details", {}) or {}
        if provider:
            details["provider"] = provider
        self.provider = provider
        super().__init__(message, details=details, **kwargs)


class ProviderUnavailableError(ProviderError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = ErrorCode.PROVIDER_UNAVAILABLE
    message = "The AI provider is currently unavailable."


class ProviderRateLimitError(ProviderError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = ErrorCode.RATE_LIMITED
    message = "The AI provider rate-limited this request."


class RetrievalError(AppError):
    code = ErrorCode.RETRIEVAL_ERROR
    message = "Retrieval failed."
    retryable = True


class GroundingError(UnprocessableError):
    """The generated answer failed grounding/citation validation (§17).

    Surfaced rather than silently returned: an ungrounded answer is worse than
    no answer in a legal context.
    """

    code = ErrorCode.GROUNDING_FAILED
    message = "The generated answer could not be grounded in the retrieved evidence."


# =============================================================================
# Handlers
# =============================================================================
def _envelope(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "trace_id": trace_id or get_trace_id(),
        }
    }


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an :class:`AppError` in the standard envelope."""
    assert isinstance(exc, AppError)
    trace_id = get_trace_id()

    log = logger.bind(
        error_code=exc.code,
        status_code=exc.status_code,
        path=request.url.path,
        method=request.method,
    )
    if exc.status_code >= 500:
        log.error("request_failed", message=exc.message, details=exc.details, exc_info=exc)
    elif isinstance(exc, ProjectAccessDeniedError | PermissionDeniedError):
        # Authorization failures are security signal - always warn.
        log.warning("authorization_denied", message=exc.message, details=exc.details)
    else:
        log.info("request_rejected", message=exc.message, details=exc.details)

    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitedError) and exc.retry_after:
        headers["Retry-After"] = str(exc.retry_after)
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"

    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_envelope(trace_id),
        headers=headers or None,
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Flatten FastAPI/Pydantic validation errors into the same envelope."""
    assert isinstance(exc, RequestValidationError)
    fields = [
        {
            "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    logger.info(
        "request_validation_failed",
        path=request.url.path,
        method=request.method,
        fields=fields,
    )
    return JSONResponse(
        status_code=422,
        content=_envelope(
            ErrorCode.VALIDATION_ERROR,
            "One or more fields are invalid.",
            {"fields": fields},
        ),
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Normalise Starlette HTTPExceptions (404 routing, 405, etc.)."""
    assert isinstance(exc, StarletteHTTPException)
    code_map = {
        401: ErrorCode.UNAUTHENTICATED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        429: ErrorCode.RATE_LIMITED,
    }
    code = code_map.get(
        exc.status_code,
        ErrorCode.BAD_REQUEST if exc.status_code < 500 else ErrorCode.INTERNAL_ERROR,
    )
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    if exc.status_code >= 500:
        logger.error("http_exception", status_code=exc.status_code, path=request.url.path)
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, detail),
        headers=getattr(exc, "headers", None),
    )


async def sqlalchemy_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map driver errors to a safe envelope - never leak SQL to the client."""
    assert isinstance(exc, SQLAlchemyError)
    if isinstance(exc, IntegrityError):
        logger.warning(
            "integrity_error",
            path=request.url.path,
            method=request.method,
            detail=str(getattr(exc, "orig", exc))[:500],
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_envelope(
                ErrorCode.CONFLICT,
                "The operation conflicts with existing data.",
            ),
        )

    logger.error("database_error", path=request.url.path, method=request.method, exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(ErrorCode.DATABASE_ERROR, "A database error occurred."),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort. Logs everything, returns nothing but a trace id."""
    logger.exception(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        exception_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(
            ErrorCode.INTERNAL_ERROR,
            "An unexpected error occurred. Quote the trace id when reporting this.",
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every handler onto the app. Called once from the app factory."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)


__all__ = [
    "AppError",
    "BadRequestError",
    "ChunkingError",
    "ConflictError",
    "CorruptedDocumentError",
    "DatabaseError",
    "DuplicateDocumentError",
    "EmbeddingError",
    "ErrorCode",
    "ExtractionError",
    "FileTooLargeError",
    "ForbiddenError",
    "GroundingError",
    "IndexingError",
    "InvalidCredentialsError",
    "InvalidStateTransitionError",
    "NotFoundError",
    "NotImplementedFeatureError",
    "OcrError",
    "ParserError",
    "ParserTimeoutError",
    "PermissionDeniedError",
    "PipelineError",
    "ProjectAccessDeniedError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
    "QueueError",
    "RateLimitedError",
    "RetrievalError",
    "SchemaValidationError",
    "StorageError",
    "TokenExpiredError",
    "TokenInvalidError",
    "UnauthenticatedError",
    "UnprocessableError",
    "UnsupportedFileTypeError",
    "ValidationError",
    "register_exception_handlers",
]
