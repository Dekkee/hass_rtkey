import asyncio
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, RTKeyCamerasApi


async def async_setup_entry(hass, config_entry, async_add_entities):
    cameras_api = hass.data[DOMAIN][config_entry.entry_id]["cameras_api"]
    entities = []

    intercoms_info = await cameras_api.get_intercoms_info()
    for intercom_info in intercoms_info["data"]["devices"]:
        camera_id = intercom_info.get("camera_id")
        camera_info = (
            await cameras_api.get_camera_info(camera_id) if camera_id else None
        )
        entities.append(
            RTKeySwitchEntity(
                hass, config_entry, cameras_api, intercom_info, camera_info
            )
        )

    barriers_info = await cameras_api.get_barriers_info()
    for barrier_info in barriers_info["data"]["devices"]:
        entities.append(
            RTKeySwitchEntity(
                hass, config_entry, cameras_api, barrier_info, None
            )
        )

    async_add_entities(entities)


class RTKeySwitchEntity(SwitchEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: RTKeyCamerasApi,
        device_info: dict,
        camera_info: dict | None,
    ) -> None:
        super().__init__()

        self.hass = hass
        self.config_entry_id = config_entry.entry_id
        self.cameras_api = cameras_api
        self.device_id = device_info["id"]
        self.camera_id = device_info.get("camera_id")  # may be None
        if camera_info:
            self.device_name = self.cameras_api.build_device_name(camera_info["title"])
        else:
            self.device_name = self.cameras_api.build_device_name(
                device_info["name_by_company"]
            )
        self._attr_unique_id = f"{self.config_entry_id}_switch_{self.device_id}"
        self._attr_name = self.device_name
        self._attr_is_on = False
        self._auto_off_task: asyncio.Task | None = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.cameras_api.open_device(self.device_id)
        self._attr_is_on = True
        if self._auto_off_task and not self._auto_off_task.done():
            self._auto_off_task.cancel()
        self._auto_off_task = asyncio.create_task(self.auto_turn_off())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        self._attr_is_on = False

    async def auto_turn_off(self) -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return
        self._attr_is_on = False
        await self.hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": self.entity_id},
            blocking=False,
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._auto_off_task and not self._auto_off_task.done():
            self._auto_off_task.cancel()

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {
                (
                    DOMAIN,
                    f"{self.config_entry_id}_{self.camera_id if self.camera_id else self.device_id}",
                )
            },
            "name": self.device_name,
        }
