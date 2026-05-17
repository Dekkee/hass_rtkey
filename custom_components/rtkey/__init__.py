import asyncio
import functools
import json
import logging
import re
import time
from urllib.parse import urlparse

import jwt
import requests
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from transliterate import translit

DOMAIN = "rtkey"

PLATFORMS: list[str] = [Platform.IMAGE, Platform.CAMERA, Platform.SWITCH]

CONF_NAME = "name"
CONF_TOKEN = "token"
CONF_CAMERA_IMAGE_REFRESH_INTERVAL = "camera_image_refresh_interval"

DATA_SCHEMA = {
    vol.Required(CONF_NAME, default="Flat1"): str,
}

OPTIONS_SCHEMA = {
    vol.Required(CONF_TOKEN): str,
    vol.Required(CONF_CAMERA_IMAGE_REFRESH_INTERVAL, default=2): int,
}

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_BUFFER = 300
RATE_LIMIT_DELAY = 120  # each api query will repeated only after this delay
HTTP_TIMEOUT = 15


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    _LOGGER.debug(
        "async_setup_entry data=%s options-keys=%s",
        config_entry.data,
        list(config_entry.options),
    )
    api = RTKeyCamerasApi(hass, config_entry)

    try:
        cameras_info = await api.get_cameras_info()
        intercoms_info = await api.get_intercoms_info()
        await api.get_barriers_info()
    except ConfigEntryAuthFailed:
        raise
    except Exception as ex:
        raise ConfigEntryNotReady(f"Failed to fetch initial data: {ex}") from ex

    await _async_migrate_unique_ids(hass, config_entry, api, cameras_info, intercoms_info)

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"cameras_api": api}
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    config_entry.async_on_unload(config_entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    res = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    if res:
        hass.data[DOMAIN].pop(config_entry.entry_id, None)
    return res


async def _async_update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(config_entry.entry_id)


async def _async_migrate_unique_ids(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    api: "RTKeyCamerasApi",
    cameras_info: dict,
    intercoms_info: dict,
) -> None:
    """Rewrite legacy name-based unique_ids to stable id-based ones.

    Why: legacy code derived unique_id from the transliterated device title,
    so renaming a device on the portal would orphan the existing entity and
    drop its automations/history.
    """
    # Legacy slugifier mirrored exactly, including the [^a-zA-z0-9] typo, so we
    # match what was actually written into the registry before the fix.
    def legacy_entity_id(device_name: str) -> str:
        return (
            DOMAIN
            + "."
            + re.sub("[^a-zA-z0-9]+", "_", device_name).rstrip("_").lower()
        )

    mapping: dict[str, str] = {}

    for camera_info in cameras_info["data"]:
        legacy = legacy_entity_id(api.build_device_name(camera_info["title"]))
        uid = camera_info["uid"]
        mapping[f"camera-{legacy}"] = f"{config_entry.entry_id}_camera_{uid}"
        mapping[f"image-{legacy}"] = f"{config_entry.entry_id}_image_{uid}"

    cameras_by_uid = {c["uid"]: c for c in cameras_info["data"]}
    for intercom_info in intercoms_info["data"]["devices"]:
        camera_id = intercom_info.get("camera_id")
        camera_info = cameras_by_uid.get(camera_id) if camera_id else None
        if camera_info:
            device_name = api.build_device_name(camera_info["title"])
        else:
            device_name = api.build_device_name(intercom_info["name_by_company"])
        legacy = legacy_entity_id(device_name)
        new = f"{config_entry.entry_id}_switch_{intercom_info['id']}"
        mapping[f"switch-{legacy}"] = new

    @callback
    def update_unique_id(entry: er.RegistryEntry) -> dict | None:
        if entry.unique_id in mapping:
            new_uid = mapping[entry.unique_id]
            _LOGGER.info(
                "Migrating unique_id for %s: %s -> %s",
                entry.entity_id,
                entry.unique_id,
                new_uid,
            )
            return {"new_unique_id": new_uid}
        return None

    await er.async_migrate_entries(hass, config_entry.entry_id, update_unique_id)


class RTKeyCamerasApi:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self.token = config_entry.options[CONF_TOKEN]
        self.config_entry_name = config_entry.data[CONF_NAME]
        self.cameras_lock = asyncio.Lock()
        self.intercoms_lock = asyncio.Lock()
        self.barriers_lock = asyncio.Lock()
        self.cached_cameras_info = None
        self.cached_cameras_info_timestamp = None
        self.cached_camera_images = {}
        self.cached_intercoms_info = None
        self.cached_intercoms_info_timestamp = None
        self.cached_barriers_info = None
        self.cached_barriers_info_timestamp = None
        self.camera_image_locks = {}
        self.camera_image_tasks = {}
        self.camera_image_refresh_interval = config_entry.options[
            CONF_CAMERA_IMAGE_REFRESH_INTERVAL
        ]

    async def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        kwargs.setdefault("allow_redirects", True)
        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {self.token}")
        r = await self.hass.async_add_executor_job(
            functools.partial(requests.request, method, url, headers=headers, **kwargs)
        )
        _LOGGER.debug("%s %s -> %s", method, url, r.status_code)
        if r.status_code == 401:
            raise ConfigEntryAuthFailed("RTKey API rejected token")
        r.raise_for_status()
        return r

    async def get_cameras_info(self) -> dict:
        async with self.cameras_lock:
            if self.cached_cameras_info:
                _LOGGER.debug("Using cached cameras info")
                return self.cached_cameras_info

            r = await self._request(
                "GET",
                "https://keyapis.key.rt.ru/vc/api/v1/camera_video_data/list?paging.limit=100&paging.offset=0",
            )

            self.cached_cameras_info = json.loads(r.content)
            self.cached_cameras_info_timestamp = int(time.time())

            for camera_info in self.cached_cameras_info["data"]:
                decoded_screenshot_token = jwt.decode(
                    camera_info["screenshotToken"], options={"verify_signature": False}
                )
                camera_info["screenshotTokenExp"] = decoded_screenshot_token["exp"]

                decoded_streamer_token = jwt.decode(
                    camera_info["streamerToken"], options={"verify_signature": False}
                )
                camera_info["streamerTokenExp"] = decoded_streamer_token["exp"]

                camera_id = camera_info["uid"]
                if camera_id not in self.cached_camera_images:
                    self.cached_camera_images[camera_id] = None
                if camera_id not in self.camera_image_locks:
                    self.camera_image_locks[camera_id] = asyncio.Lock()

            return self.cached_cameras_info

    async def clear_cached_cameras_info(self) -> None:
        async with self.cameras_lock:
            if self.cached_cameras_info:
                now = int(time.time())
                if (now - self.cached_cameras_info_timestamp) > RATE_LIMIT_DELAY:
                    self.cached_cameras_info = None
                    self.cached_cameras_info_timestamp = None

    async def get_camera_info(self, camera_id: str) -> dict | None:
        cameras_info = await self.get_cameras_info()
        for camera_info in cameras_info["data"]:
            if camera_info["uid"] == camera_id:
                return camera_info
        return None

    async def get_camera_image(self, camera_id: str) -> bytes | None:
        camera_info = await self.get_camera_info(camera_id)

        now = int(time.time())
        if (
            camera_info
            and (camera_info["screenshotTokenExp"] - now) < TOKEN_REFRESH_BUFFER
        ):
            await self.clear_cached_cameras_info()
            camera_info = await self.get_camera_info(camera_id)

        if not camera_info:
            return None

        async with self.camera_image_locks[camera_id]:
            if self.cached_camera_images[camera_id]:
                _LOGGER.debug("Using cached image for camera %s", camera_id)
                return self.cached_camera_images[camera_id]

            size = "large"
            url = camera_info["screenshotUrlTemplate"].format(
                timestamp=now, size=size, cdn_token=camera_info["screenshotToken"]
            )
            _LOGGER.debug("Fetching screenshot for camera %s", camera_id)
            r = await self._request(
                "GET",
                url,
                headers={"X-UTOKEN": camera_info["userToken"]},
            )

            self.cached_camera_images[camera_id] = r.content
            self.camera_image_tasks[camera_id] = asyncio.create_task(
                self.clear_cached_camera_image(
                    camera_id, self.camera_image_refresh_interval
                )
            )

            return r.content

    async def get_camera_stream_url(self, camera_id: str) -> str | None:
        camera_info = await self.get_camera_info(camera_id)

        now = int(time.time())
        if (
            camera_info
            and (camera_info["streamerTokenExp"] - now) < TOKEN_REFRESH_BUFFER
        ):
            await self.clear_cached_cameras_info()
            camera_info = await self.get_camera_info(camera_id)

        if not camera_info:
            return None

        camera_netloc = urlparse(camera_info["streamerUrl"]).netloc
        streamer_token = camera_info["streamerToken"]
        return f"https://{camera_netloc}/stream/{camera_id}/live.mp4?mp4-fragment-length=0.5&mp4-use-speed=0&mp4-afiller=1&token={streamer_token}"

    async def clear_cached_camera_image(self, camera_id: str, ttl: int) -> None:
        await asyncio.sleep(ttl)
        async with self.camera_image_locks[camera_id]:
            self.cached_camera_images[camera_id] = None
        _LOGGER.debug("Deleted cached image for camera %s", camera_id)

    def build_device_name(self, device_title) -> str:
        device_name = device_title.lower()
        device_name = f"{self.config_entry_name} {device_name}"
        device_name = translit(device_name, "ru", reversed=True)
        return device_name.capitalize()

    async def get_intercoms_info(self) -> dict:
        async with self.intercoms_lock:
            if self.cached_intercoms_info:
                _LOGGER.debug("Using cached intercoms info")
                return self.cached_intercoms_info

            r = await self._request(
                "GET",
                "https://household.key.rt.ru/api/v2/app/devices/intercom",
            )

            self.cached_intercoms_info = json.loads(r.content)
            self.cached_intercoms_info_timestamp = int(time.time())

            return self.cached_intercoms_info

    async def get_barriers_info(self) -> dict:
        async with self.barriers_lock:
            if self.cached_barriers_info:
                _LOGGER.debug("Using cached barriers info")
                return self.cached_barriers_info

            r = await self._request(
                "GET",
                "https://household.key.rt.ru/api/v2/app/devices/barrier",
            )

            self.cached_barriers_info = json.loads(r.content)
            self.cached_barriers_info_timestamp = int(time.time())

            return self.cached_barriers_info

    async def open_device(self, device_id) -> None:
        url = f"https://household.key.rt.ru/api/v2/app/devices/{device_id}/open"
        _LOGGER.debug("Opening device %s", device_id)
        await self._request("POST", url)
