"""Switches for this integration.

- Pair entry  -> SyncEnabledSwitch: turn the Alice→Arylic handoff on/off.
- Group entry -> ArylicGroupSwitch: join/unjoin 2+ Music Assistant speakers.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity

from . import AliceArylicConfigEntry, _is_group
from .const import CONF_GROUP_LEADER, CONF_GROUP_MEMBERS, DOMAIN

GROUP_MEMBERS_ATTR = "group_members"
UNAVAILABLE_STATES = (None, "unavailable", "unknown")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AliceArylicConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if _is_group(entry):
        async_add_entities([ArylicGroupSwitch(entry)])
    else:
        async_add_entities([SyncEnabledSwitch(entry)])


class SyncEnabledSwitch(SwitchEntity, RestoreEntity):
    """Turns the Alice -> Arylic sync on or off without removing the entry."""

    _attr_has_entity_name = True
    _attr_translation_key = "sync_enabled"
    _attr_icon = "mdi:swap-horizontal-bold"

    def __init__(self, entry: AliceArylicConfigEntry) -> None:
        self._controller = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_sync_enabled"
        self._attr_is_on = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="alice-arylic-sync",
            model="Alice ↔ Arylic Smooth Sync",
            configuration_url="https://github.com/OzodbekNormamatov-git/alice-arylic-sync",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        self._controller.enabled = bool(self._attr_is_on)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "alice_entity": self._controller.alice_entity,
            "arylic_entities": self._controller.arylic_entities,
            "last_run": self._controller.last_run,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self._controller.enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._controller.enabled = False
        # Also abort a handoff/stop already in flight (matches
        # automation.turn_off semantics, which stops the current run).
        self._controller.async_cancel()
        self.async_write_ha_state()


class ArylicGroupSwitch(SwitchEntity):
    """One toggle that joins/splits a group of Music Assistant speakers.

    ON  -> every member joins the leader (media_player.join) so they all play the
           same thing in sync.
    OFF -> every member leaves the group (media_player.unjoin) and is standalone
           again.

    The switch reflects REALITY: it recomputes its on/off state from the leader's
    live ``group_members`` attribute, so grouping/ungrouping done elsewhere (the
    speaker's own app, another automation) is shown correctly here too.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "group_enabled"
    _attr_icon = "mdi:speaker-multiple"

    def __init__(self, entry: AliceArylicConfigEntry) -> None:
        self._leader: str = entry.data[CONF_GROUP_LEADER]
        self._members: list[str] = list(entry.data[CONF_GROUP_MEMBERS])
        self._attr_unique_id = f"{entry.entry_id}_group_enabled"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="alice-arylic-sync",
            model="Arylic Speaker Group",
            configuration_url="https://github.com/nurillaevich/alice-arylic-sync",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._recompute()
        # Keep in step with grouping changes made anywhere (app, other automations).
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._leader, *self._members], self._state_changed
            )
        )

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _recompute(self) -> None:
        """On iff every member currently sits in the leader's group."""
        leader = self.hass.states.get(self._leader)
        if leader is None or leader.state in UNAVAILABLE_STATES:
            return
        group = set(leader.attributes.get(GROUP_MEMBERS_ATTR) or [])
        self._attr_is_on = bool(self._members) and all(m in group for m in self._members)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"leader": self._leader, "members": self._members}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Join every member to the leader — gaplessly, mid-playback.

        We NEVER pause or restart the leader: if it is already playing when the
        group is switched on, the join just adds the members underneath the
        running stream and they sync to it. Some LinkPlay firmwares briefly drop
        the leader out of 'playing' on join, so if it was playing before we nudge
        it straight back to play — the music continues without a manual pause and
        without the user touching anything.
        """
        leader = self.hass.states.get(self._leader)
        was_playing = leader is not None and leader.state == "playing"

        await self.hass.services.async_call(
            "media_player",
            "join",
            {"group_members": self._members},
            target={"entity_id": self._leader},
            blocking=True,
        )

        if was_playing:
            # Give the join a moment to settle, then resume the leader only if the
            # firmware actually blipped it — otherwise leave the running stream be.
            await asyncio.sleep(0.4)
            still = self.hass.states.get(self._leader)
            if still is not None and still.state != "playing":
                await self.hass.services.async_call(
                    "media_player",
                    "media_play",
                    {},
                    target={"entity_id": self._leader},
                    blocking=True,
                )

        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Split the group: every member (and the leader) becomes standalone."""
        await self.hass.services.async_call(
            "media_player",
            "unjoin",
            {},
            target={"entity_id": [self._leader, *self._members]},
            blocking=True,
        )
        self._attr_is_on = False
        self.async_write_ha_state()
