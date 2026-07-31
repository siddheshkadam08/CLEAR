"""Shared HTTP plumbing for the webhook-shaped providers.

Slack, Teams and the generic webhook differ only in the JSON body they build and
the URL they post it to. Everything else - client lifetime, timeout, what counts
as a delivery failure - is identical, and belongs in one place so the three
channels cannot drift apart.

The client is created lazily and reused. Building an ``AsyncClient`` per alert
would open a fresh TCP+TLS connection every time, which is measurable when a
batch of documents fails together and is exactly when the system is least able to
afford it.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

import httpx

from app.alerting.base import AlertEvent, IAlertProvider


class HttpAlertProvider(IAlertProvider):
    """Base for providers that deliver by POSTing JSON."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._headers = {"Content-Type": "application/json", **(headers or {})}
        #: Test seam. httpx's supported way to intercept requests without patching:
        #: passing a ``MockTransport`` exercises the real client, the real payload
        #: construction and the real status handling, and stops only at the socket.
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        return bool(self._url)

    @abstractmethod
    def build_payload(self, event: AlertEvent) -> dict[str, Any]:
        """The provider-specific request body."""

    def _get_client(self) -> httpx.AsyncClient:
        # Lazily, and not in __init__: constructing an AsyncClient outside a running
        # loop binds it to the wrong one, which surfaces much later as a confusing
        # "attached to a different loop" error under the test suite.
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, headers=self._headers, transport=self._transport
            )
        return self._client

    async def send(self, event: AlertEvent) -> None:
        response = await self._get_client().post(self._url, json=self.build_payload(event))
        # 2xx only. Chat webhooks answer 200 with a body of "invalid_payload" on
        # some errors, but a non-2xx is unambiguous and is what we retry on.
        response.raise_for_status()

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


__all__ = ["HttpAlertProvider"]
