"""Offline test: BestinHub/ConnectionManager survive an EW11 vanishing and coming back.

Home Assistant is not installed here, so its modules are stubbed. Run:  python3 -I tests/test_connection_recovery.py
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "bestin"

for name in ("homeassistant", "homeassistant.components", "homeassistant.config_entries", "homeassistant.const",
             "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.dispatcher",
             "homeassistant.helpers.event", "serial_asyncio_fast"):
    sys.modules[name] = MagicMock()
notif = MagicMock()
sys.modules["homeassistant.components"].persistent_notification = notif
sys.modules["homeassistant.core"].callback = lambda f: f
sys.modules["homeassistant.const"].CONF_PORT = "port"
sys.modules["homeassistant.const"].CONF_USERNAME = "username"

pkg = types.ModuleType("bestin"); pkg.__path__ = [str(PKG)]; sys.modules["bestin"] = pkg
for sub in ("const", "center", "controller", "until"):
    sys.modules[f"bestin.{sub}"] = MagicMock()
spec = importlib.util.spec_from_file_location("bestin.hub", PKG / "hub.py")
hub_mod = importlib.util.module_from_spec(spec); sys.modules["bestin.hub"] = hub_mod; spec.loader.exec_module(hub_mod)

_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *a, **k):  # shrink reconnect backoff (2, 4, 8 ... 60 s) for the test
    await _real_sleep(delay / 50)

hub_mod.asyncio = types.SimpleNamespace(**{k: getattr(asyncio, k) for k in dir(asyncio) if not k.startswith("__")})
hub_mod.asyncio.sleep = _fast_sleep
hub_mod.CONNECT_TIMEOUT = 1.0
hub_mod.OFFLINE_GRACE = 1.0
hub_mod.NOTIFY_AFTER = 2.0


async def serve(port: int) -> asyncio.AbstractServer:
    return await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)


async def main() -> None:
    srv = await serve(0)
    port = srv.sockets[0].getsockname()[1]
    cm = hub_mod.ConnectionManager(f"127.0.0.1:{port}")
    await cm.connect()
    assert cm.is_connected() and cm.down_for() is None

    hub = hub_mod.BestinHub(MagicMock(), MagicMock(data={"host": "127.0.0.1"}, entry_id="e1"))
    hub.connection = cm
    dev = MagicMock(); hub.api = MagicMock(devices={"d": dev})

    # EW11 vanishes: server gone, connection refused on every reconnect.
    srv.close()
    await cm._close_writer()
    for _ in range(3):
        await cm.reconnect()
    assert cm.down_for() is not None and not cm.is_connected()
    assert hub.available, "must stay available inside the grace period"
    await _real_sleep(1.1); hub._watchdog()
    assert not hub.available and dev.update_callbacks.called, "entities should flip to unavailable"
    assert not notif.async_create.called
    await _real_sleep(1.1); hub._watchdog()
    assert notif.async_create.call_count == 1, "notification after NOTIFY_AFTER"
    hub._watchdog(); assert notif.async_create.call_count == 1, "notification must not repeat"

    # EW11 comes back on the same port: the backoff loop must find it again.
    srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    for _ in range(10):
        await cm.reconnect()
        if cm.is_connected():
            break
    assert cm.is_connected() and cm.down_for() is None
    hub._watchdog()
    assert hub.available and notif.async_dismiss.called, "recovery must dismiss the notification"
    srv.close(); await cm.close()
    print("OK: vanish -> unavailable -> notification -> recover -> dismissed")

asyncio.run(main())
