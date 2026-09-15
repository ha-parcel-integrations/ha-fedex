"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

The carrier-specific :data:`_STATUS_MAP` and :func:`normalize_parcel` are
FedEx-specific. Everything else — the
timestamp parsing, the history builder, the sort contract, the delivered
filter, the one-shot warning for unmapped statuses — is suite-wide machinery
and should be left alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-fedex/issues/new"
    "?template=unrecognised_status.yml"
)

# FedEx derived status vocabulary, kept deliberately conservative.
#
# The keys are whatever the API reports; the values must come from the
# canonical enum — never invent a new one. Prefer mapping too little over
# mapping wrongly: an unmapped value surfaces as ``unknown`` plus a one-shot
# warning that asks the user to report it, which is how the map grows.
_STATUS_MAP: dict[str, ParcelStatus] = {
    # Observed scan event codes. The current parcel status still comes from
    # ``latestStatusDetail.derivedCode``; these classify timeline entries.
    "OC": ParcelStatus.REGISTERED,  # Shipment information sent / order created.
    "DO": ParcelStatus.REGISTERED,  # Dropped off; not necessarily collected yet.
    "IP": ParcelStatus.IN_TRANSIT,  # In FedEx possession.
    "DS": ParcelStatus.IN_TRANSIT,  # Vehicle dispatched.
    "PD": ParcelStatus.PROBLEM,  # Pickup delayed.
    "CA": ParcelStatus.PROBLEM,  # Cancelled; no canonical cancelled state.
    "US": ParcelStatus.IN_TRANSIT,  # Scheduled-delivery update.
    "TR": ParcelStatus.IN_TRANSIT,  # Transfer / en route to delivery.
    "DR": ParcelStatus.IN_TRANSIT,  # Vehicle furnished but not used.
    "PM": ParcelStatus.IN_TRANSIT,  # In progress.
    "MD": ParcelStatus.IN_TRANSIT,  # Manifest data.
    "CH": ParcelStatus.IN_TRANSIT,  # Location changed.
    "AC": ParcelStatus.IN_TRANSIT,  # At Canada Post facility.
    "OX": ParcelStatus.IN_TRANSIT,  # Shipment information sent to USPS.
    "CP": ParcelStatus.IN_TRANSIT,  # Clearance in progress.
    "EA": ParcelStatus.IN_TRANSIT,  # Export approved.
    "DD": ParcelStatus.PROBLEM,  # Delivery delay.
    "SE": ParcelStatus.PROBLEM,  # Shipment exception.
    "AE": ParcelStatus.IN_TRANSIT,  # Shipment arriving early.
    "AO": ParcelStatus.IN_TRANSIT,  # Shipment arriving on time.
    "DY": ParcelStatus.IN_TRANSIT,  # Delivery update.
    "AR": ParcelStatus.IN_TRANSIT,  # Arrived at a FedEx location/port.
    "AF": ParcelStatus.IN_TRANSIT,  # At a FedEx facility.
    "DP": ParcelStatus.IN_TRANSIT,  # Departed a FedEx location.
    "CC": ParcelStatus.IN_TRANSIT,  # Cleared customs.
    "HP": ParcelStatus.AT_PICKUP_POINT,  # Ready for recipient pickup.
    "CD": ParcelStatus.PROBLEM,  # Clearance delay.
    "IN": ParcelStatus.REGISTERED,  # Label created; the derived code behind OC.
    "PU": ParcelStatus.IN_TRANSIT,
    "IT": ParcelStatus.IN_TRANSIT,
    "OD": ParcelStatus.OUT_FOR_DELIVERY,
    "DL": ParcelStatus.DELIVERED,
    "DE": ParcelStatus.PROBLEM,
}

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised FedEx status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its event code when the API has no human-readable text. Sorted oldest →
    newest and capped to the most recent ``max_events``.

    FedEx scan-event fields are read from the documented response shape.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("date") or event.get("timestamp"))
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(
                event.get("eventType") or event.get("statusCode")
            ),
            "raw_status": event.get("eventDescription")
            or event.get("description")
            or event.get("eventType")
            or event.get("statusCode"),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _location_text(location: dict | None) -> str | None:
    """Return the safe city/region/country portion of a FedEx location."""
    location = location or {}
    parts = [
        location.get("city"),
        location.get("stateOrProvinceCode"),
        location.get("countryCode"),
    ]
    text = ", ".join(str(part) for part in parts if part)
    return text or None


def _weight_kg(raw: dict) -> float | None:
    """Select a metric package weight, converting pounds only if needed."""
    weights = ((raw.get("packageDetails") or {}).get("weightAndDimensions") or {}).get(
        "weight"
    ) or []
    pounds: float | None = None
    for weight in weights:
        if not isinstance(weight, dict):
            continue
        try:
            value = float(weight.get("value"))
        except (TypeError, ValueError):
            continue
        if weight.get("unit") == "KG":
            return value
        if weight.get("unit") == "LB":
            pounds = value
    return round(pounds * 0.45359237, 3) if pounds is not None else None


def _dimensions_cm(raw: dict) -> dict[str, Any] | None:
    """Select metric dimensions, converting inches only when centimetres lack."""
    dimensions = (
        (raw.get("packageDetails") or {}).get("weightAndDimensions") or {}
    ).get("dimensions") or []
    inches: tuple[float, float, float] | None = None
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            continue
        try:
            values = tuple(
                float(dimension[axis]) for axis in ("length", "width", "height")
            )
        except (KeyError, TypeError, ValueError):
            continue
        if dimension.get("units") == "CM":
            return format_dimensions(*values)
        if dimension.get("units") == "IN":
            inches = values
    if inches is None:
        return None
    return format_dimensions(*(value * 2.54 for value in inches))


def _delivered_at(raw: dict) -> str | None:
    """Read an explicit actual-delivery timestamp from known FedEx shapes."""
    delivery = raw.get("deliveryDetails") or {}
    value = delivery.get("actualDeliveryTimestamp") or raw.get(
        "actualDeliveryTimestamp"
    )
    if value:
        return to_iso_timestamp(value)
    for entry in raw.get("dateAndTimes") or []:
        if isinstance(entry, dict) and entry.get("type") == "ACTUAL_DELIVERY":
            return to_iso_timestamp(entry.get("dateTime"))
    return None


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are
    the contract**: every carrier in the suite returns exactly these, in this
    order, and the aggregator and cross-carrier dashboards depend on it. Set a
    key to ``None`` when the carrier does not expose it — never omit it.

    Rules worth keeping when you rewrite the body:

    * ``status`` is canonical, ``raw_status`` is the carrier's own text.
    * A delivered parcel has ``delivered_at`` set and ``planned_from`` /
      ``planned_to`` cleared — the ETA is meaningless once it has arrived.
    * ``planned_to`` is ``None`` for a point estimate; only fill it when the
      carrier genuinely reports a *window*.
    * ``weight`` is kilograms, ``dimensions`` centimetres (see
      :func:`format_dimensions`).
    * ``history`` is ``None`` when the option is off — the key still exists.
    """
    tracking_code = (raw.get("trackingNumberInfo") or {}).get(
        "trackingNumber"
    ) or raw.get("trackingNumber")
    detail = raw.get("latestStatusDetail") or {}
    # ``derivedCode`` is an aggregate display bucket. A live package returned
    # derived ``HP`` (hold for pickup) while its specific ``code`` was ``PU``
    # (picked up) and the consumer site correctly reported it in transit.
    # The specific code therefore always wins for the canonical lifecycle.
    status_code = (
        detail.get("code") or detail.get("derivedCode") or raw.get("statusCode")
    )
    status = map_parcel_status(status_code)
    delivered = status is ParcelStatus.DELIVERED

    window = (
        raw.get("estimatedDeliveryTimeWindow") or raw.get("estimatedDelivery") or {}
    )
    planned_from = to_iso_timestamp(
        (window.get("window") or window).get("begins") or window.get("from")
    )
    planned_to = to_iso_timestamp(
        (window.get("window") or window).get("ends") or window.get("to")
    )
    if planned_from and planned_to and parse_iso(planned_to) == parse_iso(planned_from):
        # Same instant twice is a point estimate, not a window.
        planned_to = None

    return {
        "carrier": "FedEx",
        "barcode": tracking_code,
        "sender": _location_text((raw.get("shipperInformation") or {}).get("address")),
        "receiver": _location_text(
            (raw.get("recipientInformation") or {}).get("address")
        ),
        "status": status,
        "raw_status": detail.get("statusByLocale") or status_code,
        "delivered": delivered,
        "delivered_at": _delivered_at(raw) if delivered else None,
        "planned_from": None if delivered else planned_from,
        "planned_to": None if delivered else planned_to,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": (
            _location_text(detail.get("scanLocation"))
            if status is ParcelStatus.AT_PICKUP_POINT
            else None
        ),
        "url": tracking_url(tracking_code),
        "weight": _weight_kg(raw),
        "dimensions": _dimensions_cm(raw),
        "history": build_history(raw.get("scanEvents")) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
