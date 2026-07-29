"""Alice <-> Arylic Sync: hand music off from a Yandex Station to an Arylic
speaker (via Music Assistant) with a smooth, stepped crossfade."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_entity_registry_updated_event

from .const import (
    CONF_ALICE_ENTITY,
    CONF_ARYLIC_ENTITIES,
    CONF_ARYLIC_ENTITY,
    CONF_ENTRY_TYPE,
    CONF_GROUP_LEADER,
    CONF_GROUP_MEMBERS,
    ENTRY_TYPE_GROUP,
)
from .controller import SyncController

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SWITCH]

# Group entries carry no controller (runtime_data stays None) — the group switch
# reads the leader/members straight from entry.data.
type AliceArylicConfigEntry = ConfigEntry[SyncController | None]


def _is_group(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP


def _tracked_entities(entry: ConfigEntry) -> list[str]:
    """The player entity_ids this entry depends on (for rename/remove tracking)."""
    if _is_group(entry):
        return [entry.data[CONF_GROUP_LEADER], *entry.data[CONF_GROUP_MEMBERS]]
    return [entry.data[CONF_ALICE_ENTITY], *entry.data[CONF_ARYLIC_ENTITIES]]


def _unique_id_for(data: dict) -> str:
    """Recompute the entry's unique id from its (possibly renamed) player ids."""
    if data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
        return "group__" + "__".join(
            [data[CONF_GROUP_LEADER], *sorted(data[CONF_GROUP_MEMBERS])]
        )
    return "__".join([data[CONF_ALICE_ENTITY], *sorted(data[CONF_ARYLIC_ENTITIES])])


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate v1 entries (single arylic_entity) to v2 (arylic_entities list)."""
    if entry.version == 1:
        data = {**entry.data}
        if CONF_ARYLIC_ENTITY in data:
            data[CONF_ARYLIC_ENTITIES] = [data.pop(CONF_ARYLIC_ENTITY)]
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Migrated %s to config entry version 2 (multi-room)", entry.title)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AliceArylicConfigEntry) -> bool:
    """Set up a config entry — either an Alice→Arylic pair or a speaker group."""
    if _is_group(entry):
        return await _async_setup_group_entry(hass, entry)

    controller = SyncController(hass, entry)
    entry.runtime_data = controller

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(
        async_track_entity_registry_updated_event(
            hass,
            _tracked_entities(entry),
            _make_registry_listener(hass, entry),
        )
    )

    # Forward platforms FIRST: the switch restores the previous enabled/disabled
    # state in async_added_to_hass, and the Alice listener must not start
    # handing off before that restore lands.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()
    return True


async def _async_setup_group_entry(
    hass: HomeAssistant, entry: AliceArylicConfigEntry
) -> bool:
    """Set up a speaker-group entry: just the group switch, no Alice controller."""
    entry.runtime_data = None
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(
        async_track_entity_registry_updated_event(
            hass,
            _tracked_entities(entry),
            _make_registry_listener(hass, entry),
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _make_registry_listener(hass: HomeAssistant, entry: AliceArylicConfigEntry):
    """Track entity_id renames/removals of the configured players."""

    @callback
    def _registry_updated(event: Event) -> None:
        data = event.data
        if data["action"] == "remove":
            _LOGGER.warning(
                "Entity %s was removed; the '%s' pair will not work until you "
                "re-add the integration with existing players",
                data["entity_id"],
                entry.title,
            )
            return
        if data["action"] != "update" or "old_entity_id" not in data:
            return
        old_id, new_id = data["old_entity_id"], data["entity_id"]

        def _replace(value):
            if isinstance(value, list):
                return [new_id if item == old_id else item for item in value]
            return new_id if value == old_id else value

        new_data = {key: _replace(value) for key, value in entry.data.items()}
        if new_data == dict(entry.data):
            return
        _LOGGER.info("Entity %s renamed to %s — updating the entry", old_id, new_id)
        # The update listener reloads the entry, rebinding all listeners.
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            unique_id=_unique_id_for(new_data),
        )

    return _registry_updated


async def _async_update_listener(hass: HomeAssistant, entry: AliceArylicConfigEntry) -> None:
    """Reload the entry when options or entry data change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AliceArylicConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and entry.runtime_data is not None:
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Dissolve the Music Assistant group when the integration is REMOVED.

    Reload (every option change) and disabling the Sync switch deliberately keep
    the outputs grouped and playing — only a true removal tears the group down,
    so we don't leave an orphaned group ganging the speakers together after the
    integration is deleted. async_remove_entry only fires on real removal (not on
    reload), so this is the safe place for it. A speaker-group entry is grouped by
    definition, so its removal dissolves the group the same way."""
    if _is_group(entry):
        # Members only — never the leader (its dissolve path hits the
        # SET_MEMBERS NotImplementedError on some MA providers). A group always
        # has at least one joined member, so no minimum-count guard here.
        targets = list(entry.data.get(CONF_GROUP_MEMBERS, []))
    else:
        outputs = list(entry.data.get(CONF_ARYLIC_ENTITIES, []))
        targets = outputs if len(outputs) >= 2 else []  # single output was never grouped
    if not targets:
        return
    try:
        await hass.services.async_call(
            "media_player",
            "unjoin",
            {},
            target={"entity_id": targets},
            blocking=True,
        )
        _LOGGER.info("Dissolved the Arylic group for removed entry '%s'", entry.title)
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Could not dissolve the Arylic group on removal of '%s' (the players "
            "may already be ungrouped)",
            entry.title,
        )
