"""Tests for the FedEx OAuth tracking client."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.fedex.api import (
    FedExApiClient,
    FedExApiError,
    FedExAuthError,
)
from custom_components.fedex.const import OAUTH_URL


def _response(status, body, headers=None):
    response = AsyncMock(status=status, headers=headers or {})
    response.json.return_value = body
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _session(*responses):
    session = MagicMock()
    session.post.side_effect = responses
    return session


async def test_validate_credentials_requests_and_caches_oauth_token():
    session = _session(_response(200, {"access_token": "token", "expires_in": 3600}))
    client = FedExApiClient("client", "secret", session)
    await client.async_validate_key()
    await client.async_validate_key()
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["data"]["grant_type"] == "client_credentials"


async def test_tracking_uses_bearer_and_returns_first_result():
    session = _session(
        _response(200, {"access_token": "token", "expires_in": 3600}),
        _response(
            200,
            {
                "output": {
                    "completeTrackResults": [
                        {
                            "trackResults": [
                                {"trackingNumberInfo": {"trackingNumber": "123"}}
                            ]
                        }
                    ]
                }
            },
        ),
    )
    parcel = await FedExApiClient("client", "secret", session).async_get_parcel("123")
    assert parcel["trackingNumberInfo"]["trackingNumber"] == "123"
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer token"


async def test_oauth_rejection_is_auth_error():
    client = FedExApiClient("client", "secret", _session(_response(401, {})))
    with pytest.raises(FedExAuthError):
        await client.async_validate_key()


async def test_tracking_401_reissues_token_once():
    session = _session(
        _response(200, {"access_token": "old", "expires_in": 3600}),
        _response(401, {}),
        _response(200, {"access_token": "new", "expires_in": 3600}),
        _response(200, {"output": {"completeTrackResults": []}}),
    )
    assert (
        await FedExApiClient("client", "secret", session).async_get_parcel("123")
        is None
    )
    assert session.post.call_count == 4


async def test_oauth_http_error_is_api_error():
    client = FedExApiClient("client", "secret", _session(_response(500, {})))
    with pytest.raises(FedExApiError, match="OAuth HTTP 500"):
        await client.async_validate_key()


async def test_unparseable_oauth_response_is_api_error():
    response = _response(200, None)
    response.__aenter__.return_value.json.side_effect = ValueError
    client = FedExApiClient("client", "secret", _session(response))
    with pytest.raises(FedExApiError, match="unparseable OAuth response"):
        await client.async_validate_key()


@pytest.mark.parametrize(
    "body",
    [
        {"expires_in": 3600},
        {"access_token": "", "expires_in": 3600},
        {"access_token": "token"},
        {"access_token": "token", "expires_in": "3600"},
        [],
    ],
)
async def test_incomplete_oauth_response_is_api_error(body):
    client = FedExApiClient("client", "secret", _session(_response(200, body)))
    with pytest.raises(FedExApiError, match="invalid OAuth response"):
        await client.async_validate_key()


async def test_tracking_403_is_auth_error():
    session = _session(
        _response(200, {"access_token": "token", "expires_in": 3600}),
        _response(403, {}),
    )
    with pytest.raises(FedExAuthError, match="tracking HTTP 403"):
        await FedExApiClient("client", "secret", session).async_get_parcel("123")


async def test_tracking_401_after_refresh_is_auth_error():
    session = _session(
        _response(200, {"access_token": "old", "expires_in": 3600}),
        _response(401, {}),
        _response(200, {"access_token": "new", "expires_in": 3600}),
        _response(401, {}),
    )
    with pytest.raises(FedExAuthError, match="tracking HTTP 401"):
        await FedExApiClient("client", "secret", session).async_get_parcel("123")


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"Retry-After": "90"}, 90.0), ({"Retry-After": "soon"}, None), ({}, None)],
)
async def test_tracking_429_carries_retry_after(headers, expected):
    session = _session(
        _response(200, {"access_token": "token", "expires_in": 3600}),
        _response(429, {}, headers),
    )
    with pytest.raises(FedExApiError) as err:
        await FedExApiClient("client", "secret", session).async_get_parcel("123")
    assert err.value.status_code == 429
    assert err.value.retry_after == expected


async def test_tracking_http_error_is_api_error():
    session = _session(
        _response(200, {"access_token": "token", "expires_in": 3600}),
        _response(500, {}),
    )
    with pytest.raises(FedExApiError) as err:
        await FedExApiClient("client", "secret", session).async_get_parcel("123")
    assert err.value.status_code == 500


async def test_unparseable_tracking_response_is_api_error():
    tracking = _response(200, None)
    tracking.__aenter__.return_value.json.side_effect = ValueError
    session = _session(
        _response(200, {"access_token": "token", "expires_in": 3600}), tracking
    )
    with pytest.raises(FedExApiError, match="unparseable tracking response"):
        await FedExApiClient("client", "secret", session).async_get_parcel("123")


async def test_concurrent_parcels_share_a_single_token_request():
    """Every tracked code is fetched concurrently against one client, so an
    unserialised refresh would cost one token request per parcel."""
    empty = {"output": {"completeTrackResults": []}}
    token_response = _response(200, {"access_token": "token", "expires_in": 3600})
    issued = token_response.__aenter__.return_value
    gate = asyncio.Event()

    async def _held_open():
        # Hold the token request open until every parcel has reached _token,
        # so a missing lock shows up as three OAuth calls instead of one.
        await gate.wait()
        return issued

    token_response.__aenter__ = AsyncMock(side_effect=_held_open)
    session = _session(
        token_response,
        _response(200, empty),
        _response(200, empty),
        _response(200, empty),
    )
    client = FedExApiClient("client", "secret", session)

    task = asyncio.gather(*(client.async_get_parcel(code) for code in "abc"))
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await task

    oauth_calls = [c for c in session.post.call_args_list if c.args[0] == OAUTH_URL]
    assert len(oauth_calls) == 1
    assert session.post.call_count == 4
