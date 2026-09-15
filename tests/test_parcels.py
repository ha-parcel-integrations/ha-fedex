"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fedex.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.fedex.parcels import (
    apply_delivered_filter,
    build_history,
    format_dimensions,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    tracking_url,
)

from .payloads import active_sample, delivered_sample, event, pickup_sample

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("OC", ParcelStatus.REGISTERED),
        ("IN", ParcelStatus.REGISTERED),
        ("PU", ParcelStatus.IN_TRANSIT),
        ("IT", ParcelStatus.IN_TRANSIT),
        ("OD", ParcelStatus.OUT_FOR_DELIVERY),
        ("HP", ParcelStatus.AT_PICKUP_POINT),
        ("DL", ParcelStatus.DELIVERED),
        ("DE", ParcelStatus.PROBLEM),
    ],
)
def test_map_parcel_status_known(code, expected):
    """The lifecycle codes a real parcel reports in latestStatusDetail."""
    assert map_parcel_status(code) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("OC", ParcelStatus.REGISTERED),
        ("DO", ParcelStatus.REGISTERED),
        ("PU", ParcelStatus.IN_TRANSIT),
        ("IP", ParcelStatus.IN_TRANSIT),
        ("DS", ParcelStatus.IN_TRANSIT),
        ("PD", ParcelStatus.PROBLEM),
        ("CA", ParcelStatus.PROBLEM),
        ("US", ParcelStatus.IN_TRANSIT),
        ("TR", ParcelStatus.IN_TRANSIT),
        ("DR", ParcelStatus.IN_TRANSIT),
        ("PM", ParcelStatus.IN_TRANSIT),
        ("MD", ParcelStatus.IN_TRANSIT),
        ("CH", ParcelStatus.IN_TRANSIT),
        ("AC", ParcelStatus.IN_TRANSIT),
        ("OX", ParcelStatus.IN_TRANSIT),
        ("CP", ParcelStatus.IN_TRANSIT),
        ("EA", ParcelStatus.IN_TRANSIT),
        ("DD", ParcelStatus.PROBLEM),
        ("SE", ParcelStatus.PROBLEM),
        ("AE", ParcelStatus.IN_TRANSIT),
        ("AO", ParcelStatus.IN_TRANSIT),
        ("DY", ParcelStatus.IN_TRANSIT),
        ("AR", ParcelStatus.IN_TRANSIT),
        ("AF", ParcelStatus.IN_TRANSIT),
        ("DP", ParcelStatus.IN_TRANSIT),
        ("CC", ParcelStatus.IN_TRANSIT),
        ("HP", ParcelStatus.AT_PICKUP_POINT),
        ("CD", ParcelStatus.PROBLEM),
    ],
)
def test_map_observed_fedex_scan_event_statuses(code, expected):
    """Known scan events never generate an unknown-status warning."""
    assert map_event_status(code) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("TELEPORTED") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING_NEW") is None
    assert map_event_status("DL") == ParcelStatus.DELIVERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert caplog.text.count("ABDUCTED") == 1
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_converts_epoch_milliseconds():
    assert to_iso_timestamp(1784203767167) == "2026-07-16T12:09:27.167000+00:00"
    assert to_iso_timestamp("2026-04-29T13:12:42Z") == "2026-04-29T13:12:42Z"
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(10**20) is None  # out of range -> None, never raises


def test_format_dimensions_needs_all_three_axes():
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_orders_oldest_to_newest():
    history = build_history(delivered_sample()["scanEvents"])
    assert len(history) == 4
    assert history[0]["raw_status"] == "Shipment information sent to FedEx"
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_caps_to_max_events():
    events = [
        event("IT", f"2026-04-{day:02d}T10:00:00Z", "moved")
        for day in range(1, 26)
    ]
    assert len(build_history(events, max_events=20)) == 20


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"statusCode": "IN_TRANSIT"}]) == []  # no timestamp
    assert build_history(["not-a-dict"]) == []


def test_build_history_keeps_unparseable_timestamp_last():
    history = build_history(
        [
            event("REGISTERED", "2026-04-24T10:00:00Z", "fine"),
            event("IN_TRANSIT", "not-a-date", "odd"),
        ]
    )
    assert [entry["raw_status"] for entry in history] == ["fine", "odd"]


def test_build_history_falls_back_to_status_code_without_text():
    history = build_history([event("IN_TRANSIT", "2026-04-24T10:00:00Z", "")])
    assert history[0]["raw_status"] == "IN_TRANSIT"


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample.

    Copy this test into a real carrier's own test_parcels.py verbatim — it
    stays correct for whatever subset of CAPABILITIES that carrier declares.
    """
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(active_sample())
    pickup = normalize_parcel(pickup_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "FedEx"
    assert parcel["barcode"] == "EXAMPLE123456"
    assert parcel["sender"] == "EXAMPLE CITY, CA, US"
    assert parcel["receiver"] == "EXAMPLE CITY, CA, US"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Delivered"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == to_iso_timestamp("2026-04-29T13:12:42-05:00")
    # A delivered parcel drops its ETA — the window is meaningless once it has
    # arrived.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert "fedex.com" in parcel["url"]
    assert parcel["weight"] == 1.25
    assert parcel["dimensions"] == {
        "length": 30.0,
        "width": 20.0,
        "height": 10.0,
        "text": "30 x 20 x 10 cm",
    }
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert [entry["status"] for entry in parcel["history"]] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]


def test_normalize_active_parcel_has_window():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["planned_from"] == to_iso_timestamp("2026-04-29T13:00:00-05:00")
    assert parcel["planned_to"] == to_iso_timestamp("2026-04-29T15:00:00-05:00")


def test_normalize_collapses_point_estimate_to_no_window_end():
    raw = active_sample()
    window = raw["estimatedDeliveryTimeWindow"]["window"]
    window["ends"] = window["begins"]
    parcel = normalize_parcel(raw)
    assert parcel["planned_from"] == to_iso_timestamp("2026-04-29T13:00:00-05:00")
    assert parcel["planned_to"] is None


def test_normalize_pickup_parcel():
    parcel = normalize_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "EXAMPLE CITY, CA, US"


def test_specific_latest_status_code_overrides_derived_pickup_bucket():
    """FedEx can label a picked-up package with derivedCode HP."""
    raw = {
        "trackingNumberInfo": {"trackingNumber": "TEST123"},
        "latestStatusDetail": {
            "code": "PU",
            "derivedCode": "HP",
            "statusByLocale": "We have your package",
        },
    }
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["pickup"] is False


def test_normalize_official_fields_for_label_created_package():
    """Canonical values are populated from the documented API response shape."""
    raw = {
        "trackingNumberInfo": {"trackingNumber": "TEST123"},
        "shipperInformation": {
            "address": {
                "city": "Origin",
                "stateOrProvinceCode": "CA",
                "countryCode": "US",
            }
        },
        "recipientInformation": {
            "address": {
                "city": "Destination",
                "stateOrProvinceCode": "OR",
                "countryCode": "US",
            }
        },
        "latestStatusDetail": {"code": "OC", "statusByLocale": "Label created"},
        "packageDetails": {
            "weightAndDimensions": {"weight": [{"value": "2.0", "unit": "KG"}]}
        },
        "estimatedDeliveryTimeWindow": {
            "window": {
                "begins": "2026-07-28T09:00:00-07:00",
                "ends": "2026-07-28T17:00:00-07:00",
            }
        },
        "scanEvents": [
            {
                "date": "2026-07-27T13:22:00-07:00",
                "eventType": "OC",
                "eventDescription": "Shipment information sent to FedEx",
            }
        ],
    }
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["sender"] == "Origin, CA, US"
    assert parcel["receiver"] == "Destination, OR, US"
    assert parcel["weight"] == 2.0
    assert parcel["planned_from"] == "2026-07-28T09:00:00-07:00"
    assert parcel["planned_to"] == "2026-07-28T17:00:00-07:00"
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_prefers_metric_weight_and_dimensions():
    """FedEx sends imperial first and metric second for some shipments."""
    raw = delivered_sample()
    raw["packageDetails"]["weightAndDimensions"] = {
        "weight": [{"value": "24.7", "unit": "LB"}, {"value": "11.2", "unit": "KG"}],
        "dimensions": [
            {"length": 13, "width": 12, "height": 7, "units": "IN"},
            {"length": 33, "width": 30, "height": 17, "units": "CM"},
        ],
    }

    parcel = normalize_parcel(raw)

    assert parcel["weight"] == 11.2
    assert parcel["dimensions"] == {
        "length": 33.0,
        "width": 30.0,
        "height": 17.0,
        "text": "33 x 30 x 17 cm",
    }


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-scanned code still yields a full parcel dict."""
    parcel = normalize_parcel({"trackingNumber": "EXAMPLE000001"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None


def test_normalize_blank_fields_become_none():
    raw = active_sample()
    raw["shipperInformation"]["address"] = {}
    raw["recipientInformation"]["address"] = {}
    parcel = normalize_parcel(raw)
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_normalize_keeps_full_raw_payload():
    raw = active_sample()
    assert normalize_parcel(raw)["raw"] is raw


def test_normalize_falls_back_to_status_code_without_text():
    raw = active_sample()
    raw["latestStatusDetail"]["statusByLocale"] = None
    assert normalize_parcel(raw)["raw_status"] == "OD"


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


def test_tracking_url_needs_a_code():
    assert tracking_url(None) is None
    assert tracking_url("") is None


def _with_weight(*weights) -> dict:
    return {"packageDetails": {"weightAndDimensions": {"weight": list(weights)}}}


def test_weight_prefers_kilograms():
    parcel = normalize_parcel(
        _with_weight({"value": "5", "unit": "LB"}, {"value": "2.5", "unit": "KG"})
    )
    assert parcel["weight"] == 2.5


def test_weight_converts_pounds_when_no_metric_entry():
    assert normalize_parcel(_with_weight({"value": "10", "unit": "LB"}))["weight"] == (
        4.536
    )


def test_weight_skips_unusable_entries():
    parcel = normalize_parcel(
        _with_weight("nonsense", {"value": None, "unit": "KG"}, {"unit": "KG"})
    )
    assert parcel["weight"] is None


def _with_dimensions(*dimensions) -> dict:
    return {"packageDetails": {"weightAndDimensions": {"dimensions": list(dimensions)}}}


def test_dimensions_prefers_centimetres():
    parcel = normalize_parcel(
        _with_dimensions(
            {"length": 10, "width": 10, "height": 10, "units": "IN"},
            {"length": 30, "width": 20, "height": 10, "units": "CM"},
        )
    )
    assert parcel["dimensions"] == format_dimensions(30, 20, 10)


def test_dimensions_convert_inches_when_no_metric_entry():
    parcel = normalize_parcel(
        _with_dimensions({"length": 10, "width": 5, "height": 2, "units": "IN"})
    )
    assert parcel["dimensions"] == format_dimensions(25.4, 12.7, 5.08)


def test_dimensions_skip_unusable_entries():
    parcel = normalize_parcel(
        _with_dimensions("nonsense", {"length": 10, "width": 5, "units": "CM"})
    )
    assert parcel["dimensions"] is None


def _delivered(extra: dict) -> dict:
    return {"latestStatusDetail": {"code": "DL"}, **extra}


def test_delivered_at_reads_the_explicit_timestamp():
    parcel = normalize_parcel(
        _delivered(
            {"deliveryDetails": {"actualDeliveryTimestamp": "2026-09-14T10:00:00Z"}}
        )
    )
    assert parcel["delivered_at"] == to_iso_timestamp("2026-09-14T10:00:00Z")


def test_delivered_at_falls_back_to_date_and_times():
    parcel = normalize_parcel(
        _delivered(
            {
                "dateAndTimes": [
                    "nonsense",
                    {"type": "SHIP", "dateTime": "2026-09-12T08:00:00Z"},
                    {"type": "ACTUAL_DELIVERY", "dateTime": "2026-09-14T10:00:00Z"},
                ]
            }
        )
    )
    assert parcel["delivered_at"] == to_iso_timestamp("2026-09-14T10:00:00Z")


def test_delivered_at_is_none_without_a_timestamp():
    assert normalize_parcel(_delivered({}))["delivered_at"] is None
