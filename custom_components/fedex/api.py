"""FedEx Basic Integrated Visibility API client."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from .const import OAUTH_URL, TRACKING_API_URL


class FedExApiError(Exception):
    """A non-authentication FedEx API failure."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store error metadata for coordinator retry handling."""
        super().__init__(f"FedEx API request failed: {detail}")
        self.status_code = status_code
        self.retry_after = retry_after


class FedExAuthError(FedExApiError):
    """FedEx rejected the configured OAuth client credentials."""


class FedExApiClient:
    """OAuth client-credentials transport; access tokens stay memory-only."""

    def __init__(
        self, client_id: str, client_secret: str, session: aiohttp.ClientSession
    ) -> None:
        """Initialise a client for one user-owned FedEx OAuth project."""
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._token_lock = asyncio.Lock()

    def _cached_token(self, stale: str | None) -> str | None:
        if (
            self._access_token
            and self._access_token != stale
            and self._expires_at
            and self._expires_at > datetime.now(timezone.utc)
        ):
            return self._access_token
        return None

    async def _token(self, *, stale: str | None = None) -> str:
        # Every tracked code is fetched concurrently against one client, so an
        # unserialised refresh would fire one token request per parcel.
        if (cached := self._cached_token(stale)) is not None:
            return cached
        async with self._token_lock:
            if (cached := self._cached_token(stale)) is not None:
                return cached
            return await self._refresh_token()

    async def _refresh_token(self) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        async with self._session.post(OAUTH_URL, data=data) as response:
            if response.status in (401, 403):
                raise FedExAuthError(
                    f"OAuth HTTP {response.status}", status_code=response.status
                )
            if response.status != 200:
                raise FedExApiError(
                    f"OAuth HTTP {response.status}", status_code=response.status
                )
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise FedExApiError("unparseable OAuth response") from err
        token = payload.get("access_token") if isinstance(payload, dict) else None
        expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(expires_in, (int, float))
        ):
            raise FedExApiError("invalid OAuth response")
        self._access_token = token
        self._expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(0, expires_in - 60)
        )
        return token

    async def async_validate_key(self) -> None:
        """Validate credentials without consuming a package lookup."""
        await self._token()

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Return the matching result, retrying once with a fresh token."""
        stale: str | None = None
        for attempt in range(2):
            token = await self._token(stale=stale)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-locale": "en_US",
            }
            body = {
                "includeDetailedScans": True,
                "trackingInfo": [
                    {"trackingNumberInfo": {"trackingNumber": tracking_code}}
                ],
            }
            async with self._session.post(
                TRACKING_API_URL, headers=headers, json=body
            ) as response:
                if response.status == 401 and attempt == 0:
                    stale = token
                    continue
                if response.status in (401, 403):
                    raise FedExAuthError(
                        f"tracking HTTP {response.status}", status_code=response.status
                    )
                if response.status == 429:
                    try:
                        retry_after = float(response.headers.get("Retry-After", ""))
                    except ValueError:
                        retry_after = None
                    raise FedExApiError(
                        "HTTP 429", status_code=429, retry_after=retry_after
                    )
                if response.status != 200:
                    raise FedExApiError(
                        f"tracking HTTP {response.status}", status_code=response.status
                    )
                try:
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    raise FedExApiError("unparseable tracking response") from err
            results = (
                payload.get("output", {}).get("completeTrackResults", [])
                if isinstance(payload, dict)
                else []
            )
            if not isinstance(results, list) or not results:
                return None
            first = results[0] if isinstance(results[0], dict) else {}
            track_results = first.get("trackResults", [])
            return (
                track_results[0]
                if isinstance(track_results, list)
                and track_results
                and isinstance(track_results[0], dict)
                else None
            )
        raise FedExAuthError(  # pragma: no cover - the retry above always returns or raises
            "tracking token refresh failed"
        )
