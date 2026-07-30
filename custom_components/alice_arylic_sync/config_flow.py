"""Config flow: pick the Alice and Arylic players in the UI; every tuning
parameter lives in the options flow (the integration's Configure dialog)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ALICE_ENTITY,
    CONF_ARYLIC_ENTITIES,
    CONF_ENTRY_TYPE,
    CONF_GROUP_LEADER,
    CONF_GROUP_MEMBERS,
    DEFAULTS,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_PAIR,
    OPT_ALICE_END_VOLUME,
    OPT_ALICE_GAP_MS,
    OPT_ALICE_RESTORE_VOLUME,
    OPT_ARYLIC_FLOOR,
    OPT_ARYLIC_TARGET,
    OPT_BUFFER_WAIT_TIMEOUT,
    OPT_HANDOFF_DELAY,
    OPT_HEADSTART_MS,
    OPT_MEDIA_TYPE,
    OPT_PLAY_WAIT_TIMEOUT,
    OPT_REDIRECT_VOLUME,
    OPT_REGROUP_EACH_TRACK,
    OPT_SEEK_EACH_OUTPUT,
    OPT_SKIP_SEEK,
    OPT_STEPS,
    OPT_STOP_CONFIRM_DELAY,
    OPT_STOP_FADE_FLOOR,
    OPT_STOP_STEP_DELAY_MS,
    OPT_STOP_STEPS,
    OPT_SYNC_OFFSET,
    OPT_TRACK_URI_PREFIX,
)


def _volume_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=1, step=0.01, mode=selector.NumberSelectorMode.SLIDER
        )
    )


def _number(min_: float, max_: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        min=min_, max=max_, step=step, mode=selector.NumberSelectorMode.BOX
    )
    # unit_of_measurement must be absent (not None) when there is no unit —
    # the selector schema rejects None values.
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _group_unique_id(leader: str, members: list[str]) -> str:
    """Stable id for a speaker group (order-independent members)."""
    return "group__" + "__".join([leader, *sorted(members)])


class AliceArylicSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Initial setup: pick what to add — an Alice→Arylic pair or a speaker group."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let the user choose which kind of entry to add."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["pair", "group"],
        )

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add an Alice → Arylic(s) handoff pair (one or more outputs = multi-room)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            outputs: list[str] = user_input[CONF_ARYLIC_ENTITIES]
            if not outputs:
                errors["base"] = "no_outputs"
            elif user_input[CONF_ALICE_ENTITY] in outputs:
                errors["base"] = "same_entity"
            elif not self.hass.services.has_service("music_assistant", "play_media"):
                errors["base"] = "music_assistant_missing"
            else:
                data = {**user_input, CONF_ENTRY_TYPE: ENTRY_TYPE_PAIR}
                unique_id = "__".join(
                    [user_input[CONF_ALICE_ENTITY], *sorted(outputs)]
                )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._make_title(user_input), data=data
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ALICE_ENTITY): selector.EntitySelector(
                    # Source = a Yandex Station (Alice) only, so the list is not
                    # cluttered with every other media_player (Arylic, TVs, …).
                    selector.EntitySelectorConfig(
                        domain="media_player", integration="yandex_station"
                    )
                ),
                vol.Required(CONF_ARYLIC_ENTITIES): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="media_player",
                        integration="music_assistant",
                        multiple=True,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="pair", data_schema=schema, errors=errors)

    async def async_step_group(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add a speaker group: one switch that joins 2+ MA speakers on demand."""
        errors: dict[str, str] = {}

        if user_input is not None:
            leader: str = user_input[CONF_GROUP_LEADER]
            members: list[str] = user_input[CONF_GROUP_MEMBERS]
            if not members:
                errors["base"] = "no_members"
            elif leader in members:
                errors["base"] = "leader_in_members"
            else:
                data = {
                    CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
                    CONF_GROUP_LEADER: leader,
                    CONF_GROUP_MEMBERS: members,
                }
                await self.async_set_unique_id(_group_unique_id(leader, members))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._make_group_title(data), data=data
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_GROUP_LEADER): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="media_player", integration="music_assistant"
                    )
                ),
                vol.Required(CONF_GROUP_MEMBERS): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="media_player",
                        integration="music_assistant",
                        multiple=True,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="group", data_schema=schema, errors=errors)

    def _name(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return state.name if state else entity_id

    def _make_title(self, user_input: dict[str, Any]) -> str:
        outputs = " + ".join(self._name(e) for e in user_input[CONF_ARYLIC_ENTITIES])
        return f"{self._name(user_input[CONF_ALICE_ENTITY])} → {outputs}"

    def _make_group_title(self, data: dict[str, Any]) -> str:
        members = " + ".join(self._name(e) for e in data[CONF_GROUP_MEMBERS])
        return f"👥 {self._name(data[CONF_GROUP_LEADER])} + {members}"

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return AliceArylicSyncOptionsFlow()


class AliceArylicSyncOptionsFlow(config_entries.OptionsFlow):
    """All tuning parameters."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        # Group entries have no crossfade tuning — nothing to configure.
        if self.config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
            return self.async_abort(reason="group_no_options")

        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = {**DEFAULTS, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(OPT_SYNC_OFFSET, default=current[OPT_SYNC_OFFSET]): _number(
                    -10, 30, 0.1, "s"
                ),
                vol.Required(OPT_HANDOFF_DELAY, default=current[OPT_HANDOFF_DELAY]): _number(
                    0, 600, 1, "s"
                ),
                vol.Required(OPT_STEPS, default=current[OPT_STEPS]): _number(2, 50, 1),
                vol.Required(OPT_HEADSTART_MS, default=current[OPT_HEADSTART_MS]): _number(
                    0, 2000, 10, "ms"
                ),
                vol.Required(OPT_ALICE_GAP_MS, default=current[OPT_ALICE_GAP_MS]): _number(
                    0, 2000, 10, "ms"
                ),
                vol.Required(OPT_ARYLIC_FLOOR, default=current[OPT_ARYLIC_FLOOR]): _volume_selector(),
                vol.Required(OPT_ARYLIC_TARGET, default=current[OPT_ARYLIC_TARGET]): _volume_selector(),
                vol.Required(
                    OPT_ALICE_END_VOLUME, default=current[OPT_ALICE_END_VOLUME]
                ): _volume_selector(),
                vol.Required(
                    OPT_STOP_FADE_FLOOR, default=current[OPT_STOP_FADE_FLOOR]
                ): _volume_selector(),
                vol.Required(
                    OPT_ALICE_RESTORE_VOLUME, default=current[OPT_ALICE_RESTORE_VOLUME]
                ): _volume_selector(),
                vol.Required(OPT_STOP_STEPS, default=current[OPT_STOP_STEPS]): _number(
                    1, 40, 1
                ),
                vol.Required(
                    OPT_STOP_STEP_DELAY_MS, default=current[OPT_STOP_STEP_DELAY_MS]
                ): _number(0, 1000, 10, "ms"),
                vol.Required(
                    OPT_STOP_CONFIRM_DELAY, default=current[OPT_STOP_CONFIRM_DELAY]
                ): _number(0, 10, 0.1, "s"),
                vol.Required(
                    OPT_PLAY_WAIT_TIMEOUT, default=current[OPT_PLAY_WAIT_TIMEOUT]
                ): _number(1, 60, 1, "s"),
                vol.Required(
                    OPT_BUFFER_WAIT_TIMEOUT, default=current[OPT_BUFFER_WAIT_TIMEOUT]
                ): _number(0, 30, 1, "s"),
                vol.Required(
                    OPT_REGROUP_EACH_TRACK,
                    default=current[OPT_REGROUP_EACH_TRACK],
                ): selector.BooleanSelector(),
                vol.Required(
                    OPT_SEEK_EACH_OUTPUT,
                    default=current[OPT_SEEK_EACH_OUTPUT],
                ): selector.BooleanSelector(),
                vol.Required(
                    OPT_SKIP_SEEK,
                    default=current[OPT_SKIP_SEEK],
                ): selector.BooleanSelector(),
                vol.Required(
                    OPT_REDIRECT_VOLUME,
                    default=current[OPT_REDIRECT_VOLUME],
                ): selector.BooleanSelector(),
                vol.Required(
                    OPT_TRACK_URI_PREFIX, default=current[OPT_TRACK_URI_PREFIX]
                ): selector.TextSelector(),
                vol.Required(OPT_MEDIA_TYPE, default=current[OPT_MEDIA_TYPE]): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
