"""HTTP middleware: request context, access logging, metrics, security headers,
rate limiting and upload size enforcement.

Order matters. The app factory installs them so the outermost is the request
context (everything downstream can log with correlation) and the innermost is
rate limiting (so a rejected request is still logged and measured):

    RequestContext → SecurityHeaders → BodySizeLimit → RateLimit → router
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core import metrics
from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.core.logging import (
    bind_context,
    get_logger,
    get_trace_id,
    new_request_id,
    reset_context,
)

logger = get_logger(__name__)

Handler = Callable[[Request], Awaitable[Response]]

#: Excluded from access logs and HTTP metrics - probes would drown real traffic.
_QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics", "/favicon.ico"})


def _route_template(request: Request) -> str:
    """``/api/v1/contracts/{id}`` rather than the concrete id.

    Metric labels must be bounded; raw paths would create a series per contract.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind log context, emit the access log and metrics."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        request_id = request.headers.get("x-request-id") or new_request_id()
        request.state.request_id = request_id

        tokens = bind_context(request_id=request_id)
        started = time.perf_counter()
        quiet = request.url.path in _QUIET_PATHS

        if not quiet:
            metrics.http_requests_in_flight.inc()

        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.perf_counter() - started
            if not quiet:
                metrics.http_requests_in_flight.dec()
                route = _route_template(request)
                try:
                    metrics.http_requests_total.labels(
                        method=request.method,
                        route=route,
                        status_class=metrics.status_class(status_code),
                    ).inc()
                    metrics.http_request_duration_seconds.labels(
                        method=request.method, route=route
                    ).observe(duration)
                # Suppressed deliberately: a metrics backend problem must never turn
                # a successful response into a 500.
                except Exception:  # noqa: BLE001
                    logger.debug("request_metrics_failed", route=route)

                # Access log. 5xx is logged by the exception handler with detail;
                # here we record the outcome so every request has exactly one
                # access record.
                log = logger.bind(
                    method=request.method,
                    path=request.url.path,
                    route=route,
                    status_code=status_code,
                    duration_ms=round(duration * 1000, 2),
                    client_ip=client_ip(request),
                )
                if status_code >= 500:
                    log.error("http_request")
                elif status_code >= 400:
                    log.info("http_request")
                else:
                    log.info("http_request")

            reset_context(tokens)


class ResponseHeadersMiddleware(BaseHTTPMiddleware):
    """Attach correlation and security headers to every response."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        response = await call_next(request)
        headers = MutableHeaders(scope=None, raw=response.raw_headers)

        request_id = getattr(request.state, "request_id", None)
        if request_id:
            headers["X-Request-ID"] = request_id
        headers["X-Trace-ID"] = get_trace_id()

        # OWASP baseline. The SPA is served separately, so a strict CSP here only
        # needs to protect the API's own responses (Swagger UI needs inline).
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")

        settings = get_settings()
        if settings.is_production:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path not in {"/docs", "/redoc", "/openapi.json"}:
            headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
        return response


class BodySizeLimitMiddleware:
    """Reject oversized uploads from the ``Content-Length`` header.

    Pure ASGI (not ``BaseHTTPMiddleware``) so the rejection happens before the
    body is buffered - the point is to avoid reading 2 GB into memory at all.
    Requests without a ``Content-Length`` (chunked) are enforced downstream by
    the upload service as it streams.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:  # type: ignore[type-arg]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        # Batch upload: the endpoint accepts many files in one request.
        limit = settings.upload.max_upload_size_bytes * settings.upload.max_files_per_upload

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        response = JSONResponse(
                            status_code=413,
                            content={
                                "error": {
                                    "code": ErrorCode.FILE_TOO_LARGE,
                                    "message": (
                                        "Request body exceeds the maximum allowed upload size."
                                    ),
                                    "details": {"max_bytes": limit},
                                    "trace_id": get_trace_id(),
                                }
                            },
                        )
                        await response(scope, receive, send)
                        return
                except ValueError:
                    pass
                break

        await self.app(scope, receive, send)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Redis-backed fixed-window rate limiter.

    Keyed by authenticated user when a bearer token is present, otherwise by
    client IP. Login gets its own tighter bucket so credential stuffing is
    throttled independently of normal API traffic.

    Fail-open by design: if Redis is down, the platform keeps serving. An
    availability outage of the limiter must not become an outage of the product.
    """

    #: (path predicate, settings attribute holding the "N/period" string)
    _BUCKETS: tuple[tuple[str, str], ...] = (
        ("/auth/login", "rate_limit_login"),
        ("/auth/refresh", "rate_limit_login"),
        ("/auth/oidc", "rate_limit_login"),
    )

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        settings = get_settings()
        if not settings.security.rate_limit_enabled or request.url.path in _QUIET_PATHS:
            return await call_next(request)

        scope_name, limit, window = self._resolve_bucket(request)
        identity = self._identity(request)
        key = f"ratelimit:{scope_name}:{identity}"

        allowed, retry_after = await self._consume(key, limit, window)
        if not allowed:
            metrics.rate_limit_rejections_total.labels(scope=scope_name).inc()
            logger.warning(
                "rate_limited",
                scope=scope_name,
                identity=identity,
                path=request.url.path,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": ErrorCode.RATE_LIMITED,
                        "message": "Too many requests. Please slow down.",
                        "details": {"retry_after_seconds": retry_after},
                        "trace_id": get_trace_id(),
                    }
                },
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)

    def _resolve_bucket(self, request: Request) -> tuple[str, int, int]:
        settings = get_settings()
        spec = settings.security.rate_limit_default
        scope_name = "default"
        for fragment, attr in self._BUCKETS:
            if fragment in request.url.path:
                spec = getattr(settings.security, attr)
                scope_name = attr.replace("rate_limit_", "")
                break
        limit, window = _parse_rate(spec)
        return scope_name, limit, window

    @staticmethod
    def _identity(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            # Hash the token rather than storing it in a Redis key.
            from hashlib import sha256

            return "u:" + sha256(auth[7:].encode()).hexdigest()[:24]
        return "ip:" + client_ip(request)

    @staticmethod
    async def _consume(key: str, limit: int, window: int) -> tuple[bool, int]:
        try:
            from app.core.cache import get_redis

            redis = await get_redis()
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.ttl(key)
            count, ttl = await pipe.execute()

            if int(count) == 1 or int(ttl) < 0:
                await redis.expire(key, window)
                ttl = window

            if int(count) > limit:
                return False, max(int(ttl), 1)
            return True, 0
        except Exception as exc:  # noqa: BLE001 - fail open, see docstring
            logger.debug("rate_limit_unavailable", error=str(exc))
            return True, 0


def _parse_rate(spec: str) -> tuple[int, int]:
    """``"120/minute"`` -> ``(120, 60)``."""
    periods = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
    try:
        count, _, period = spec.partition("/")
        return int(count), periods.get(period.strip().lower(), 60)
    except (ValueError, AttributeError):
        return 120, 60


def client_ip(request: Request) -> str:
    """Best-effort client IP, honouring the first hop in ``X-Forwarded-For``.

    Only trustworthy behind a proxy that overwrites the header; Uvicorn is
    started with ``--proxy-headers`` and a restricted ``--forwarded-allow-ips``.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


__all__ = [
    "BodySizeLimitMiddleware",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
    "ResponseHeadersMiddleware",
    "client_ip",
]
