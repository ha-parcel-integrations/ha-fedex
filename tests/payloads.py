"""Sample FedEx API payloads shared by the test modules.

These model one ``output.completeTrackResults[].trackResults[]`` member as the
official tracking API really returns it — the field names, nesting and value
formats were taken from live production responses and every value was then
replaced with a synthetic one. Keep them in one module rather than inline in
each test: when the payload shape turns out to be different from what you
assumed, there is then exactly one place to fix.

Two shapes are extrapolated rather than observed, because no tracked parcel
was in that state: an out-for-delivery ``latestStatusDetail``, and a populated
``estimatedDeliveryTimeWindow`` (every live parcel returned ``{"window": {}}``,
so the ETA sensor and calendar have never had real data to show).
"""

from __future__ import annotations

ACTIVE_CODE = "EXAMPLE999999"
DELIVERED_CODE = "EXAMPLE123456"

_ADDRESS = {
    "city": "EXAMPLE CITY",
    "stateOrProvinceCode": "CA",
    "countryCode": "US",
    "residential": False,
    "countryName": "United States",
}


def event(
    event_type: str,
    date: str,
    description: str,
    *,
    derived: str | None = None,
) -> dict:
    """One entry of the carrier's own scan timeline."""
    return {
        "date": date,
        "eventType": event_type,
        "eventDescription": description,
        "exceptionCode": "",
        "exceptionDescription": "",
        "scanLocation": dict(_ADDRESS),
        "locationType": "FEDEX_FACILITY",
        "derivedStatusCode": derived or event_type,
        "derivedStatus": description,
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative tracking result for a delivered parcel."""
    return {
        "trackingNumberInfo": {
            "trackingNumber": code,
            "trackingNumberUniqueId": "0000~EXAMPLE~FDEG",
            "carrierCode": "FDXG",
        },
        "shipperInformation": {"contact": {}, "address": dict(_ADDRESS)},
        "recipientInformation": {"contact": {}, "address": dict(_ADDRESS)},
        "latestStatusDetail": {
            "code": "DL",
            "derivedCode": "DL",
            "statusByLocale": "Delivered",
            "description": "Delivered",
            "scanLocation": dict(_ADDRESS),
        },
        "dateAndTimes": [
            {"type": "ACTUAL_DELIVERY", "dateTime": "2026-04-29T13:12:42-05:00"},
            {"type": "ACTUAL_PICKUP", "dateTime": "2026-04-27T23:03:58-05:00"},
            {"type": "SHIP", "dateTime": "2026-04-27T00:00:00+00:00"},
        ],
        "packageDetails": {
            "packagingDescription": {"type": "YOUR_PACKAGING", "description": "Package"},
            "weightAndDimensions": {
                "weight": [
                    {"value": "2.76", "unit": "LB"},
                    {"value": "1.25", "unit": "KG"},
                ],
                "dimensions": [
                    {"length": 12, "width": 8, "height": 4, "units": "IN"},
                    {"length": 30, "width": 20, "height": 10, "units": "CM"},
                ],
            },
        },
        "scanEvents": [
            event("DL", "2026-04-29T13:12:42-05:00", "Delivered"),
            event(
                "OD",
                "2026-04-29T08:46:00-05:00",
                "On FedEx vehicle for delivery",
                derived="IT",
            ),
            event(
                "AR", "2026-04-28T15:52:17-05:00", "Arrived at FedEx hub", derived="IT"
            ),
            event(
                "OC",
                "2026-04-27T23:03:58-05:00",
                "Shipment information sent to FedEx",
                derived="IN",
            ),
        ],
        "estimatedDeliveryTimeWindow": {"window": {}},
        "standardTransitTimeWindow": {
            "window": {"ends": "2026-04-29T17:00:00-05:00"}
        },
        "serviceDetail": {"type": "GROUND_HOME_DELIVERY", "description": "Home Delivery"},
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel with an ETA window."""
    sample = delivered_sample(code)
    sample.update(
        {
            "latestStatusDetail": {
                "code": "OD",
                "derivedCode": "OD",
                "statusByLocale": "On the way",
                "description": "On FedEx vehicle for delivery",
                "scanLocation": dict(_ADDRESS),
            },
            "dateAndTimes": [
                {"type": "ACTUAL_PICKUP", "dateTime": "2026-04-27T23:03:58-05:00"},
                {"type": "SHIP", "dateTime": "2026-04-27T00:00:00+00:00"},
            ],
            "estimatedDeliveryTimeWindow": {
                "window": {
                    "begins": "2026-04-29T13:00:00-05:00",
                    "ends": "2026-04-29T15:00:00-05:00",
                }
            },
            "scanEvents": sample["scanEvents"][1:],
        }
    )
    return sample


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel held at a FedEx location for the recipient to collect."""
    sample = active_sample(code)
    sample.update(
        {
            "latestStatusDetail": {
                "code": "HP",
                "derivedCode": "HP",
                "statusByLocale": "Ready for recipient pickup",
                "description": "Ready for recipient pickup",
                "scanLocation": dict(_ADDRESS),
            },
            "scanEvents": [
                event("HP", "2026-04-29T09:12:00-05:00", "Ready for recipient pickup"),
                *sample["scanEvents"],
            ],
        }
    )
    return sample
