"""
tests/test_platform_charge_alerts.py

Tests for:
1. PaymentRequest dataclass resilience (no TypeError on partial arguments).
2. Render keep-alive payment link generation full response payload.
3. Superadmin alerting on charge failures (via Telegram and platform audit log).
4. Sanitized error messages (no leakage of 'Render logs' or internal commands).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from httpx import AsyncClient, ASGITransport

from renewise.services.payment import PaymentRequest
from renewise.utils.alerts import alert_payment_failure, notify_superadmins
from renewise.miniapp.server import app


def test_payment_request_no_type_error_with_partial_fields():
    """Ensure PaymentRequest can be initialized without wallet URLs without raising TypeError."""
    req = PaymentRequest(
        payment_url="ton://transfer/EQ123",
        vault_address="EQ123",
        required_nano=1000000,
        payload="EQ123",
        amount=1.0,
        currency="GRAM",
    )
    assert req.payment_url == "ton://transfer/EQ123"
    assert req.tg_wallet_url == ""
    assert req.tonkeeper_url == ""
    assert req.mytonwallet_url == ""
    assert req.tonhub_url == ""
    assert req.required_nano == 1000000


@pytest.mark.asyncio
async def test_notify_superadmins_dispatches_telegram():
    """Test that notify_superadmins calls Telegram sendMessage for all configured superadmins."""
    with patch("renewise.config.SUPERADMIN_BOT_TOKEN", "fake_bot_token"), \
         patch("renewise.config.ALLOWED_SUPERADMIN_IDS", [111111, 222222]), \
         patch("aiohttp.ClientSession.post") as mock_post:
        
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_post.return_value.__aenter__.return_value = mock_resp

        await notify_superadmins("<b>Test Alert</b>")
        assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_alert_payment_failure_calls_notify_superadmins():
    """Test alert_payment_failure formats error and alerts superadmin."""
    platform = {
        "id": 42,
        "platform_name": "Test Telegram Game",
        "owner_telegram_id": 99999,
        "auth_mode": "test",
    }
    with patch("renewise.utils.alerts.notify_superadmins", new_callable=AsyncMock) as mock_notify, \
         patch("renewise.db.connection._db") as mock_db:
        
        # Mock DB context manager
        mock_conn = AsyncMock()
        mock_db.return_value.__aenter__.return_value = mock_conn

        await alert_payment_failure(
            platform=platform,
            charge_id=101,
            amount_usd_cents=500,
            error=TypeError("PaymentRequest missing arguments"),
            details="Traceback details",
        )

        assert mock_notify.call_count == 1
        msg = mock_notify.call_args[0][0]
        assert "Test Telegram Game" in msg
        assert "TypeError" in msg
        assert "101" in msg
        assert "$5.00 USD" in msg


from renewise.api.platform import authenticate_platform


@pytest.mark.asyncio
async def test_create_charge_sanitized_error_and_alert_on_proxy_failure():
    """Test that when proxying to internal service fails, superadmin is alerted and user sees clean error."""
    mock_platform = {
        "id": 1,
        "platform_name": "My Bot App",
        "owner_telegram_id": 123456,
        "wallet_address": "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJKZ",
        "auth_mode": "test",
        "buyer_fee_bps": None,
        "admin_fee_bps": None,
        "status": "active",
    }

    app.dependency_overrides[authenticate_platform] = lambda: mock_platform

    try:
        with patch("renewise.api.platform._db") as mock_db, \
             patch.dict("os.environ", {
                 "INTERNAL_API_URL": "http://fake-render-service.internal",
                 "INTERNAL_API_SECRET": "test_secret",
             }), \
             patch("renewise.utils.alerts.alert_payment_failure", new_callable=AsyncMock) as mock_alert, \
             patch("aiohttp.ClientSession.post", side_effect=Exception("Connection refused")):

            mock_conn = AsyncMock()
            mock_conn.fetchrow.side_effect = [
                None,                # SELECT id from platform_charges (duplicate check -> None)
                {"id": 999},         # INSERT INTO platform_charges RETURNING id -> 999
            ]
            mock_db.return_value.__aenter__.return_value = mock_conn

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/platform/charges",
                    headers={"Authorization": "Bearer sk_test_validkey123"},
                    json={"amount_usd_cents": 500, "external_reference": "ref_test_01"},
                )

            assert resp.status_code == 503
            data = resp.json()
            detail = data.get("detail", "")
            # The user must NOT see "Check Render logs"
            assert "Render logs" not in detail
            assert "TypeError" not in detail
            assert "npm run build" not in detail
            # Must notify superadmins
            assert mock_alert.call_count == 1
            assert "administration team has been notified" in detail
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_charge_successful_proxy_reconstruction():
    """Test that a successful proxy response deserializes all PaymentRequest fields without error."""
    mock_platform = {
        "id": 1,
        "platform_name": "My Bot App",
        "owner_telegram_id": 123456,
        "wallet_address": "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJKZ",
        "auth_mode": "test",
        "buyer_fee_bps": 200,
        "admin_fee_bps": 330,
        "status": "active",
    }

    proxy_response_data = {
        "payment_url": "ton://transfer/EQVault123?bin=te6ccgEBAQEAAgAAAA==",
        "tg_wallet_url": "ton://transfer/EQVault123",
        "tonkeeper_url": "https://app.tonkeeper.com/transfer/EQVault123",
        "mytonwallet_url": "https://app.mytonwallet.org/transfer/EQVault123",
        "tonhub_url": "https://tonhub.com/transfer/EQVault123",
        "vault_address": "EQVault123",
        "payload": "EQVault123",
        "amount": 2.5,
        "currency": "GRAM",
        "required_nano": 2500000000,
    }

    app.dependency_overrides[authenticate_platform] = lambda: mock_platform

    try:
        with patch("renewise.api.platform._db") as mock_db, \
             patch.dict("os.environ", {
                 "INTERNAL_API_URL": "http://fake-render-service.internal",
                 "INTERNAL_API_SECRET": "test_secret",
             }), \
             patch("aiohttp.ClientSession.post") as mock_post:

            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value=proxy_response_data)
            mock_post.return_value.__aenter__.return_value = mock_resp

            mock_conn = AsyncMock()
            mock_conn.fetchrow.side_effect = [
                None,                # duplicate check -> None
                {"id": 888},         # INSERT RETURNING id -> 888
            ]
            mock_conn.execute.return_value = None
            mock_db.return_value.__aenter__.return_value = mock_conn

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/platform/charges",
                    headers={"Authorization": "Bearer sk_test_validkey123"},
                    json={"amount_usd_cents": 500, "external_reference": "ref_test_02"},
                )

            assert resp.status_code == 200
            res_data = resp.json()
            assert res_data["id"] == 888
            assert res_data["payment_url"] == proxy_response_data["payment_url"]
            assert res_data["vault_address"] == "EQVault123"
            assert res_data["required_nano"] == 2500000000
    finally:
        app.dependency_overrides.clear()
