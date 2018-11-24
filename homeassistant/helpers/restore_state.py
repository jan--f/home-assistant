"""Support for restoring entity states on startup."""
import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional  # noqa  pylint_disable=unused-import

from homeassistant.core import HomeAssistant, callback, State, CoreState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STOP)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.storage import Store  # noqa  pylint_disable=unused-import

DATA_RESTORE_STATE_TASK = 'restore_state_task'

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = 'core.restore_state'
STORAGE_VERSION = 1

# How long between periodically saving the current states to disk
STATE_DUMP_INTERVAL = timedelta(minutes=15)


class RestoreStateData():
    """Helper class for managing the helper saved data."""

    @staticmethod
    async def async_get_instance(hass: HomeAssistant) -> 'RestoreStateData':
        """Get the singleton instance of this data helper."""
        task = hass.data.get(DATA_RESTORE_STATE_TASK)

        if task is None:
            async def load_instance(hass: HomeAssistant) -> 'RestoreStateData':
                """Set up the restore state helper."""
                data = RestoreStateData(hass)

                try:
                    states = await data.store.async_load()
                except HomeAssistantError as exc:
                    _LOGGER.error("Error loading last states", exc_info=exc)
                    states = None

                if states is None:
                    _LOGGER.debug('Not creating cache - no saved states found')
                    data.last_states = {}
                else:
                    data.last_states = {
                        state['entity_id']: State.from_dict(state)
                        for state in states}
                    _LOGGER.debug(
                        'Created cache with %s', list(data.last_states))

                if hass.state == CoreState.running:
                    data.async_setup_dump()
                else:
                    hass.bus.async_listen_once(
                        EVENT_HOMEASSISTANT_START, data.async_setup_dump)

                return data

            task = hass.data[DATA_RESTORE_STATE_TASK] = hass.async_create_task(
                load_instance(hass))

        return await task

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the restore state data class."""
        self.hass = hass  # type: HomeAssistant
        self.store = Store(hass, STORAGE_VERSION, STORAGE_KEY,
                           encoder=JSONEncoder)  # type: Store
        self.last_states = {}  # type: Dict[str, State]
        self.entities = []  # type: List[RestoreEntity]

    async def async_dump_states(self) -> None:
        """Save the current state machine to storage."""
        _LOGGER.debug("Dumping states")
        # Entity ID set of registered restorable entities to dump
        entity_ids = set(entity.entity_id for entity in self.entities)
        try:
            await self.store.async_save([
                state.as_dict() for state in self.hass.states.async_all()
                if state.entity_id in entity_ids])
        except HomeAssistantError as exc:
            _LOGGER.error("Error saving current states", exc_info=exc)

    @callback
    def async_setup_dump(self, *args: Any) -> None:
        """Set up the restore state listeners."""
        # Dump the initial states now. This helps minimize the risk of having
        # old states loaded by overwritting the last states once home assistant
        # has started and the old states have been read.
        self.hass.async_create_task(self.async_dump_states())

        # Dump states periodically
        async_track_time_interval(
            self.hass, lambda *_: self.hass.async_create_task(
                self.async_dump_states()), STATE_DUMP_INTERVAL)

        # Dump states when stopping hass
        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, lambda *_: self.hass.async_create_task(
                self.async_dump_states()))

    @callback
    def async_register_entity(
            self, entity: 'RestoreEntity') -> None:
        """Store this entity's state when hass is shutdown."""
        self.entities.append(entity)

    @callback
    def async_unregister_entity(
            self, entity: 'RestoreEntity') -> None:
        """Unregister this entity from saving state."""
        self.entities.remove(entity)


class RestoreEntity(Entity):
    """Mixin class for restoring previous entity state."""

    async def async_added_to_hass(self) -> None:
        """Register this entity as a restorable entity."""
        _, data = await asyncio.gather(
            super().async_added_to_hass(),
            RestoreStateData.async_get_instance(self.hass),
        )
        data.async_register_entity(self)

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        _, data = await asyncio.gather(
            super().async_will_remove_from_hass(),
            RestoreStateData.async_get_instance(self.hass),
        )
        data.async_unregister_entity(self)

    async def async_get_last_state(self) -> Optional[State]:
        """Get the entity state from the previous run."""
        if self.hass is None or self.entity_id is None:
            # Return None if this entity isn't added to hass yet
            _LOGGER.warning("Cannot get last state. Entity not added to hass")
            return None
        data = await RestoreStateData.async_get_instance(self.hass)
        return data.last_states.get(self.entity_id)
