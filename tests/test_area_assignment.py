"""Area initialization must not undo Home Assistant registry edits on reload."""

from __future__ import annotations

import asyncio
from enum import Enum
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "orvibohomebridge" / "__init__.py"
)
DOMAIN = "orvibohomebridge"


def _module(name, **values):
    module = ModuleType(name)
    module.__dict__.update(values)
    return module


class _TransportMode(str, Enum):
    AUTO = "auto"


def _load_integration():
    """Execute the real integration entry point and selection helpers."""
    name = "orvibohomebridge_area_test"
    area_registry = _module(
        "homeassistant.helpers.area_registry", async_get=lambda hass: hass.areas
    )
    device_registry = _module(
        "homeassistant.helpers.device_registry", async_get=lambda hass: hass.devices
    )
    modules = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
        ),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
        "homeassistant.const": _module("homeassistant.const", CONF_USERNAME="username"),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions",
            ConfigEntryAuthFailed=type("AuthFailed", (Exception,), {}),
            ConfigEntryNotReady=type("NotReady", (Exception,), {}),
        ),
        "homeassistant.helpers": _module(
            "homeassistant.helpers",
            area_registry=area_registry, device_registry=device_registry,
            config_validation=SimpleNamespace(empty_config_schema=lambda domain: {}),
        ),
        f"{name}.capabilities": _module(
            f"{name}.capabilities", TransportMode=_TransportMode
        ),
        f"{name}.protocol": _module(
            f"{name}.protocol", migrate_password_credentials=lambda data: data
        ),
        f"{name}.models": _module(f"{name}.models", AccountCredentials=SimpleNamespace),
        f"{name}.cloud": _module(
            f"{name}.cloud", cloud_for_region=lambda region: object()
        ),
        f"{name}.coordinator": _module(
            f"{name}.coordinator", OrviboMeshCoordinator=MagicMock()
        ),
        f"{name}.service_handlers": _module(
            f"{name}.service_handlers", async_register_services=AsyncMock()
        ),
    }
    spec = importlib.util.spec_from_file_location(
        name, MODULE_PATH, submodule_search_locations=[str(MODULE_PATH.parent)]
    )
    assert spec is not None and spec.loader is not None
    integration = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**modules, name: integration}):
        spec.loader.exec_module(integration)
    return integration


class _AreaRegistry:
    def __init__(self):
        self.areas = {}
        self.async_create = MagicMock(side_effect=self._create)

    def _create(self, name):
        area = SimpleNamespace(id=f"area-{len(self.areas) + 1}", name=name)
        self.areas[area.id] = area
        return area

    def async_get_area(self, area_id):
        return self.areas.get(area_id)

    def async_get_area_by_name(self, name):
        return next(
            (area for area in self.areas.values() if area.name == name), None
        )


class _DeviceRegistry:
    def __init__(self):
        self.devices = {}
        self.async_update_device = MagicMock(side_effect=self._update)

    def register(self, device_id, area_id=None):
        return self.devices.setdefault(
            device_id, SimpleNamespace(id=device_id, area_id=area_id)
        )

    def async_get_device(self, *, identifiers):
        return next(
            (
                self.devices[key] for domain, key in identifiers
                if domain == DOMAIN and key in self.devices
            ),
            None,
        )

    def _update(self, device_id, *, area_id):
        self.devices[device_id].area_id = area_id
        return self.devices[device_id]


class TestAreaAssignment(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_integration()

    def setUp(self):
        self.tasks = []
        self.cloud_devices = {"lamp": {"room_name": "阳台"}}
        self.registered_on_forward = {"lamp": None}
        self.entry = SimpleNamespace(
            entry_id="orvibo-entry",
            data={"username": "test", "password_hash": "hash", "cloud_region": "china"},
            options={}, async_on_unload=MagicMock(), add_update_listener=MagicMock(),
        )
        self.hass = SimpleNamespace(
            data={}, areas=_AreaRegistry(), devices=_DeviceRegistry(),
            async_create_task=self._create_task,
            config_entries=SimpleNamespace(
                async_update_entry=MagicMock(), async_reload=AsyncMock(),
                async_forward_entry_setups=AsyncMock(side_effect=self._forward),
                async_unload_platforms=AsyncMock(return_value=True),
            ),
        )

    def _create_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def _forward(self, entry, platforms):
        selected = self.module.selected_device_ids(entry.options, self.cloud_devices)
        for device_id, area_id in self.registered_on_forward.items():
            if device_id in selected:
                self.hass.devices.register(device_id, area_id)

    async def _setup(self):
        coordinator = SimpleNamespace(
            devices=self.cloud_devices, last_update_success=True,
            https_client=SimpleNamespace(
                cloud=SimpleNamespace(region=SimpleNamespace(value="china"))
            ),
            _async_setup=AsyncMock(), async_refresh=AsyncMock(), async_cleanup=AsyncMock(),
            start_history_cleanup=MagicMock(), stop_history_cleanup=MagicMock(),
            start_temp_password_cleanup=MagicMock(), stop_temp_password_cleanup=MagicMock(),
        )
        with patch.object(self.module, "OrviboMeshCoordinator", return_value=coordinator):
            self.assertTrue(await self.module.async_setup_entry(self.hass, self.entry))
            await asyncio.gather(*self.tasks)
        self.tasks.clear()

    async def _reload(self):
        self.assertTrue(await self.module.async_unload_entry(self.hass, self.entry))
        await self._setup()

    async def test_first_import_creates_cloud_area(self):
        await self._setup()
        area = self.hass.areas.async_get_area_by_name("阳台")
        self.assertIsNotNone(area)
        self.assertEqual(self.hass.devices.devices["lamp"].area_id, area.id)

    async def test_reload_preserves_manual_move_despite_legacy_mapping(self):
        await self._setup()
        old_area = self.hass.devices.devices["lamp"].area_id
        new_area = self.hass.areas.async_create("玄关")
        self.hass.devices.async_update_device("lamp", area_id=new_area.id)
        self.entry.options = {"device_areas": {"lamp": old_area}}
        self.hass.devices.async_update_device.reset_mock()
        await self._reload()
        self.assertEqual(self.hass.devices.devices["lamp"].area_id, new_area.id)
        self.hass.devices.async_update_device.assert_not_called()

    async def test_reload_does_not_recreate_deleted_area_or_reassign_device(self):
        await self._setup()
        old_area = self.hass.devices.devices["lamp"].area_id
        del self.hass.areas.areas[old_area]
        self.hass.devices.async_update_device("lamp", area_id=None)
        self.entry.options = {"device_areas": {"lamp": old_area}}
        self.hass.areas.async_create.reset_mock()
        await self._reload()
        self.assertIsNone(self.hass.devices.devices["lamp"].area_id)
        self.assertIsNone(self.hass.areas.async_get_area_by_name("阳台"))
        self.hass.areas.async_create.assert_not_called()

    async def test_reload_preserves_renamed_area(self):
        await self._setup()
        area_id = self.hass.devices.devices["lamp"].area_id
        self.hass.areas.areas[area_id].name = "书房"
        self.hass.areas.async_create.reset_mock()
        await self._reload()
        self.assertEqual(self.hass.devices.devices["lamp"].area_id, area_id)
        self.assertIsNone(self.hass.areas.async_get_area_by_name("阳台"))
        self.hass.areas.async_create.assert_not_called()

    async def test_reload_initializes_new_device_without_moving_existing_device(self):
        await self._setup()
        area = self.hass.areas.async_create("玄关")
        self.hass.devices.async_update_device("lamp", area_id=area.id)
        self.cloud_devices["curtain"] = {"room_name": "客厅"}
        self.registered_on_forward["curtain"] = None
        self.hass.areas.async_create.reset_mock()
        await self._reload()
        self.assertEqual(self.hass.devices.devices["lamp"].area_id, area.id)
        curtain_area = self.hass.areas.async_get_area_by_name("客厅")
        self.assertIsNotNone(curtain_area)
        self.assertEqual(self.hass.devices.devices["curtain"].area_id, curtain_area.id)
        self.hass.areas.async_create.assert_called_once_with("客厅")

    async def test_existing_unassigned_device_stays_unassigned(self):
        self.hass.devices.register("lamp")
        await self._setup()
        self.assertIsNone(self.hass.devices.devices["lamp"].area_id)
        self.hass.areas.async_create.assert_not_called()

    async def test_new_device_area_set_by_platform_is_preserved(self):
        area = self.hass.areas.async_create("客厅")
        self.registered_on_forward["lamp"] = area.id
        self.hass.areas.async_create.reset_mock()
        await self._setup()
        self.assertEqual(self.hass.devices.devices["lamp"].area_id, area.id)
        self.hass.areas.async_create.assert_not_called()

    async def test_unselected_or_unregistered_devices_do_not_create_areas(self):
        self.cloud_devices = {
            "hidden": {"room_name": "默认房间"}, "unsupported": {"room_name": "阳台"}
        }
        self.entry.options = {"selected_device_ids": ["unsupported"]}
        self.registered_on_forward = {}
        await self._setup()
        self.hass.areas.async_create.assert_not_called()
        self.hass.devices.async_update_device.assert_not_called()

    async def test_new_device_uses_valid_legacy_area_without_creating_cloud_area(self):
        area = self.hass.areas.async_create("玄关")
        self.entry.options = {"device_areas": {"lamp": area.id}}
        self.hass.areas.async_create.reset_mock()
        await self._setup()
        self.assertEqual(self.hass.devices.devices["lamp"].area_id, area.id)
        self.hass.areas.async_create.assert_not_called()

    async def test_new_device_explicit_none_or_deleted_legacy_area_stays_unassigned(self):
        for area_id in (None, "deleted-area"):
            with self.subTest(area_id=area_id):
                self.setUp()
                self.entry.options = {"device_areas": {"lamp": area_id}}
                await self._setup()
                self.assertIsNone(self.hass.devices.devices["lamp"].area_id)
                self.hass.areas.async_create.assert_not_called()

    async def test_options_listener_only_reloads_without_registry_writes(self):
        await self._setup()
        self.hass.devices.async_update_device("lamp", area_id=None)
        self.hass.devices.async_update_device.reset_mock()
        self.hass.areas.async_create.reset_mock()
        await self.module._async_update_listener(self.hass, self.entry)
        self.hass.config_entries.async_reload.assert_awaited_once_with(self.entry.entry_id)
        self.hass.devices.async_update_device.assert_not_called()
        self.hass.areas.async_create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
