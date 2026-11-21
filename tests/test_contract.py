"""The parcel contract's checks, run over every normalised sample."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.fedex.const import ParcelStatus
from custom_components.fedex.parcels import (
    is_pickup,
    normalize_parcel,
    planned_for_status,
    planned_window,
)

from .payloads import active_sample, delivered_sample, event, pickup_sample

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

_AMS = ZoneInfo("Europe/Amsterdam")


def _collected() -> dict:
    sample = delivered_sample()
    sample["scanEvents"] = [
        event("DL", "2026-04-29T16:00:00-05:00", "Delivered"),
        event("HP", "2026-04-29T09:12:00-05:00", "Ready for recipient pickup"),
    ]
    return sample


def _moment_only() -> dict:
    sample = active_sample()
    window = sample["estimatedDeliveryTimeWindow"]["window"]
    window["ends"] = window["begins"]
    return sample


def _returning() -> dict:
    sample = active_sample()
    sample["latestStatusDetail"]["code"] = "RS"
    sample["latestStatusDetail"]["derivedCode"] = "RS"
    return sample


SAMPLES = {
    "active": active_sample,
    "at_pickup_point": pickup_sample,
    "collected": _collected,
    "delivered": delivered_sample,
    "moment_only": _moment_only,
    "returning": _returning,
    "bare": lambda: {"trackingNumber": "1"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert datetime.fromisoformat(parcel["planned_to"]) > datetime.fromisoformat(
            parcel["planned_from"]
        )


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_names_and_places_are_not_published_as_parties(parcel):
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["pickup_point"] is None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = normalize_parcel(_collected())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True


def test_a_home_delivery_is_not_pickup():
    assert normalize_parcel(delivered_sample())["pickup"] is False


def test_a_moment_ends_at_the_end_of_its_day_in_its_own_offset():
    parcel = normalize_parcel(_moment_only())
    assert parcel["planned_to"] == "2026-04-29T23:59:59-05:00"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T15:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            None,
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            date(2026, 4, 29),
            date(2026, 4, 29),
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            "2026-05-01",
            ("2026-04-29T00:00:00+02:00", "2026-05-01T23:59:59+02:00"),
        ),
        (
            "2026-04-29T15:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            None,
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            datetime(2026, 4, 29, 13, 0),
            datetime(2026, 4, 29, 15, 0),
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (None, None, (None, None)),
        ("garbage", None, (None, None)),
        (12, 13, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=_AMS) == expected


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_planned_for_status_empties_the_window(status):
    assert planned_for_status(status, "a", "b") == (None, None)


def test_planned_for_status_keeps_the_window_otherwise():
    for status in (
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.UNKNOWN,
    ):
        assert planned_for_status(status, "a", "b") == ("a", "b")


def test_is_pickup_sources():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(False, ParcelStatus.IN_TRANSIT, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(None, ParcelStatus.DELIVERED) is False
