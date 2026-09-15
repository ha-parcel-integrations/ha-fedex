"""Tests for FedEx device triggers."""

from unittest.mock import AsyncMock

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.helpers.trigger import TriggerInfo

from custom_components.fedex.const import DOMAIN
from custom_components.fedex.device_trigger import (
    TRIGGER_EVENTS,
    async_attach_trigger,
    async_get_triggers,
)


async def test_get_triggers_returns_all_four(hass):
    triggers = await async_get_triggers(hass, "device123")
    types = {t["type"] for t in triggers}
    assert types == {
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    }
    for trigger in triggers:
        assert trigger["domain"] == DOMAIN
        assert trigger["device_id"] == "device123"


def test_trigger_events_map_to_domain_prefix():
    assert TRIGGER_EVENTS["parcel_registered"] == f"{DOMAIN}_parcel_registered"


async def test_attach_trigger_fires_action_on_matching_event(hass):
    """The device trigger delegates to the generic event trigger, filtered
    to this device_id — attaching it and firing the event must call through."""
    action = AsyncMock()
    config = {
        CONF_PLATFORM: "device",
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: "device123",
        CONF_TYPE: "parcel_delivered",
    }
    remove = await async_attach_trigger(
        hass,
        config,
        action,
        TriggerInfo(trigger_data={"id": "0", "idx": "0"}, variables={}),
    )

    hass.bus.async_fire(
        f"{DOMAIN}_parcel_delivered", {"device_id": "device123", "barcode": "X"}
    )
    await hass.async_block_till_done()

    action.assert_called_once()
    remove()
