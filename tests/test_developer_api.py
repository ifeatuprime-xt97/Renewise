"""
tests/test_developer_api.py

Tests for developer API endpoints including:
  PUT /api/developer/platforms/{platform_id}/wallet
  POST /api/developer/platforms/{platform_id}/passcode
"""
from __future__ import annotations
import asyncio
import pytest
from httpx import AsyncClient
from typing import AsyncGenerator
import urllib.parse
import hmac
import hashlib
import time

from renewise.miniapp.server import app

def generate_telegram_auth(user_id: int, bot_token: str = "fake_bot_token") -> str:
    """Helper to generate fake but valid Telegram initData for tests."""
    user_json = f'{{"id": {user_id}, "first_name": "Test", "language_code": "en"}}'
    auth_date = int(time.time())
    
    data_dict = {
        "user": user_json,
        "auth_date": str(auth_date),
        "query_id": "test_query_id"
    }
    
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_dict.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    hash_val = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    
    data_dict["hash"] = hash_val
    return urllib.parse.urlencode(data_dict)


import httpx

@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_update_wallet_unauthorized(client: AsyncClient):
    """Test updating wallet without auth fails."""
    response = await client.put(
        "/api/developer/platforms/1/wallet",
        json={"wallet_address": "EQ123..."}
    )
    assert response.status_code in (401, 403, 400, 422) # depending on auth middleware


@pytest.mark.asyncio
async def test_set_passcode_unauthorized(client: AsyncClient):
    """Test setting passcode without auth fails."""
    response = await client.post(
        "/api/developer/platforms/1/passcode",
        json={"new_passcode": "1234"}
    )
    assert response.status_code in (401, 403, 400, 422)
