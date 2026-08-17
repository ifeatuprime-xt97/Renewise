"""
tests/test_keepalive.py

Unit tests for Render keep-alive settings, health handler, and ping loop.

    python -m pytest tests/test_keepalive.py -v --asyncio-mode=auto
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from renewise.keepalive import (
    _normalize_ping_url,
    handle_health,
    load_settings,
    ping_loop,
    reset_started_for_tests,
    start_keepalive_background,
    _build_app,
)


@pytest.fixture(autouse=True)
def _reset_keepalive_guard():
    reset_started_for_tests()
    yield
    reset_started_for_tests()


def test_normalize_bare_origin_appends_health():
    assert _normalize_ping_url("https://bot.onrender.com") == "https://bot.onrender.com/health"
    assert _normalize_ping_url("https://bot.onrender.com/") == "https://bot.onrender.com/health"


def test_normalize_keeps_explicit_health_paths():
    assert _normalize_ping_url("https://api.onrender.com/healthz") == "https://api.onrender.com/healthz"
    assert _normalize_ping_url("https://api.onrender.com/health") == "https://api.onrender.com/health"
    assert _normalize_ping_url("https://api.onrender.com/ping") == "https://api.onrender.com/ping"


def test_load_settings_idle_when_nothing_configured():
    settings = load_settings({})
    assert settings.enabled is True
    assert settings.port == 0
    assert settings.urls == ()
    assert settings.should_run is False


def test_load_settings_render_defaults():
    settings = load_settings({
        "PORT": "10000",
        "RENDER_EXTERNAL_URL": "https://renewise-bot.onrender.com",
    })
    assert settings.should_run is True
    assert settings.port == 10000
    assert settings.urls == ("https://renewise-bot.onrender.com/health",)
    assert settings.interval == 600


def test_load_settings_extra_urls_deduped_and_normalized():
    settings = load_settings({
        "KEEP_ALIVE_URL": "https://bot.onrender.com/health",
        "KEEP_ALIVE_URLS": "https://bot.onrender.com/, https://api.onrender.com/healthz",
        "KEEP_ALIVE_INTERVAL": "120",
    })
    assert settings.urls == (
        "https://bot.onrender.com/health",
        "https://api.onrender.com/healthz",
    )
    assert settings.interval == 120


def test_load_settings_disabled_still_serves_port():
    """KEEP_ALIVE=false stops self-ping but still binds $PORT for Render."""
    settings = load_settings({
        "PORT": "10000",
        "KEEP_ALIVE": "false",
        "RENDER_EXTERNAL_URL": "https://bot.onrender.com",
    })
    assert settings.enabled is False
    assert settings.should_ping is False
    assert settings.should_serve is True
    assert settings.should_run is True


def test_load_settings_interval_floor():
    settings = load_settings({"KEEP_ALIVE_INTERVAL": "5"})
    assert settings.interval == 30


def test_load_settings_invalid_port_and_interval():
    settings = load_settings({
        "PORT": "not-a-port",
        "KEEP_ALIVE_INTERVAL": "nope",
    })
    assert settings.port == 0
    assert settings.interval == 600


def test_config_ignores_invalid_port_values(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("PORT", "6acAo+LSknqqOJSQAToyKqumKc66koXH2Dx83kPcYTw=")
    monkeypatch.delenv("KEEP_ALIVE_PORT", raising=False)
    sys.modules.pop("renewise.config", None)

    config = importlib.import_module("renewise.config")

    assert config.KEEP_ALIVE_PORT == 0


@pytest.mark.asyncio
async def test_health_endpoints():
    async with TestClient(TestServer(_build_app())) as client:
        for path in ("/", "/health", "/healthz"):
            resp = await client.get(path)
            assert resp.status == 200
            body = await resp.json()
            assert body == {"status": "ok", "service": "renewise-bot"}


@pytest.mark.asyncio
async def test_handle_health_direct():
    resp = await handle_health(MagicMock(spec=web.Request))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_ping_loop_hits_every_url_then_sleeps():
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.__aenter__.return_value = response
    response.__aexit__.return_value = False
    session.get.return_value = response

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()

    with patch("renewise.keepalive.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await ping_loop(
                ("https://bot.onrender.com/health", "https://api.onrender.com/healthz"),
                interval=600,
                session=session,
                initial_delay=5.0,
            )

    assert sleep_calls[0] == 5.0
    assert sleep_calls[1] == 600
    assert session.get.call_count == 2
    session.get.assert_any_call("https://bot.onrender.com/health")
    session.get.assert_any_call("https://api.onrender.com/healthz")


@pytest.mark.asyncio
async def test_start_keepalive_background_is_idempotent_and_skips_idle():
    assert await start_keepalive_background(load_settings({})) is None

    settings = load_settings({"KEEP_ALIVE_URLS": "https://bot.onrender.com/health"})
    first = await start_keepalive_background(settings)
    assert first is not None
    second = await start_keepalive_background(settings)
    assert second is None
    first.cancel()
    try:
        await first
    except (asyncio.CancelledError, Exception):
        pass
