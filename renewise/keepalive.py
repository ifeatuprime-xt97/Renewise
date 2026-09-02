"""
Render keep-alive for the Telegram bots.

Render free web services spin down after 15 minutes with no inbound HTTP.
These bots poll Telegram outbound, so without an HTTP surface they sleep
and stop receiving updates.

This module:
  1. Serves GET /, /health, /healthz on $PORT (Render requires a bound port).
  2. Periodically GETs the public service URL so Render sees inbound traffic.

On Render, PORT and RENDER_EXTERNAL_URL are set automatically. Locally and
on a VPS neither is set, so this is a no-op.

Standalone pinger (no bot process required):

    KEEP_ALIVE_URLS=https://bot.onrender.com/health,https://api.onrender.com/healthz \\
        python -m renewise.keepalive
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout, web

log = logging.getLogger("keepalive")

_HEALTH_PATHS = ("/health", "/healthz", "/ping", "/status", "/alive")
_USER_AGENT = "ReneWise-KeepAlive/1.0"
_started = False


@dataclass(frozen=True)
class KeepAliveSettings:
    enabled: bool
    port: int
    urls: tuple[str, ...]
    interval: int

    @property
    def should_serve(self) -> bool:
        """Always bind $PORT when set — Render kills web services that don't."""
        return self.port > 0

    @property
    def should_ping(self) -> bool:
        return self.enabled and bool(self.urls)

    @property
    def should_run(self) -> bool:
        return self.should_serve or self.should_ping


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def _normalize_ping_url(url: str) -> str:
    """Append /health when the caller passed a bare service origin."""
    url = url.strip().rstrip("/")
    if not url:
        return url
    lower = url.lower()
    if any(lower.endswith(path) for path in _HEALTH_PATHS):
        return url
    return url + "/health"


def load_settings(
    environ: dict[str, str] | None = None,
) -> KeepAliveSettings:
    env = os.environ if environ is None else environ

    port_raw = env.get("KEEP_ALIVE_PORT") or env.get("PORT") or "10000"
    try:
        port = int(port_raw)
    except ValueError:
        port = 0

    interval_raw = env.get("KEEP_ALIVE_INTERVAL", "600")
    try:
        interval = int(interval_raw)
    except ValueError:
        interval = 600
    interval = max(30, interval)

    urls: list[str] = []
    primary = (env.get("KEEP_ALIVE_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip()
    if primary:
        urls.append(_normalize_ping_url(primary))

    extra = env.get("KEEP_ALIVE_URLS", "")
    for item in extra.split(","):
        normalized = _normalize_ping_url(item)
        if normalized and normalized not in urls:
            urls.append(normalized)

    return KeepAliveSettings(
        enabled=_truthy(env.get("KEEP_ALIVE"), default=True),
        port=port,
        urls=tuple(urls),
        interval=interval,
    )


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "renewise-bot"})


async def handle_generate_payment_link(request: web.Request) -> web.Response:
    """
    Internal endpoint: generate a TON payment link for a platform charge.

    Called by the Mini App API (Vercel) which cannot run the contract locally.
    Protected by INTERNAL_API_SECRET — never expose this to the public.

    POST /internal/generate-payment-link
    Headers: X-Internal-Secret: <INTERNAL_API_SECRET>
    Body: {
        "platform": { ...platform dict with wallet_address, buyer_fee_bps, etc. },
        "charge_id": 42,
        "price_usd_cents": 999
    }
    Response: {
        "payment_url": "ton://...",
        "vault_address": "EQ...",
        "required_nano": 1234567890
    }
    """
    import os
    expected_secret = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected_secret:
        return web.json_response({"error": "INTERNAL_API_SECRET not configured"}, status=503)

    provided = request.headers.get("X-Internal-Secret", "")
    if not provided or provided != expected_secret:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    platform = body.get("platform")
    charge_id = body.get("charge_id")
    price_usd_cents = body.get("price_usd_cents")

    if not platform or not charge_id or not price_usd_cents:
        return web.json_response({"error": "Missing required fields"}, status=400)

    try:
        from renewise.services.payment import generate_platform_payment_request
        result = await generate_platform_payment_request(
            platform=platform,
            charge_id=charge_id,
            price_usd_cents=price_usd_cents,
        )
        return web.json_response({
            "payment_url":   result.payment_url,
            "vault_address": result.vault_address,
            "required_nano": result.required_nano,
        })
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except FileNotFoundError:
        return web.json_response(
            {"error": "Contract not compiled. Run: cd contracts && npm run build"},
            status=503,
        )
    except Exception as e:
        log.exception("Payment link generation failed for charge %s", charge_id)
        return web.json_response(
            {"error": f"Payment link generation failed: {type(e).__name__}"},
            status=500,
        )


def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/healthz", handle_health)
    app.router.add_post("/internal/generate-payment-link", handle_generate_payment_link)
    return app


async def _ping_once(session: ClientSession, url: str) -> None:
    try:
        async with session.get(url) as resp:
            log.info("keep-alive ping %s → %s", url, resp.status)
    except (ClientError, asyncio.TimeoutError, OSError) as exc:
        log.warning("keep-alive ping failed %s: %s", url, exc)


async def ping_loop(
    urls: tuple[str, ...] | list[str],
    interval: int,
    *,
    session: ClientSession | None = None,
    initial_delay: float = 5.0,
) -> None:
    if not urls:
        return

    owns_session = session is None
    if session is None:
        session = ClientSession(
            timeout=ClientTimeout(total=60),
            headers={"User-Agent": _USER_AGENT},
        )

    try:
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        while True:
            await asyncio.gather(*(_ping_once(session, url) for url in urls))
            await asyncio.sleep(interval)
    finally:
        if owns_session:
            await session.close()


async def run_keepalive(settings: KeepAliveSettings | None = None) -> None:
    """Bind the health server and/or run the ping loop until cancelled."""
    settings = settings or load_settings()
    if not settings.should_run:
        log.info("Keep-alive idle — disabled or no PORT / URLs configured.")
        return

    if settings.should_ping and settings.interval >= 15 * 60:
        log.warning(
            "KEEP_ALIVE_INTERVAL=%ss is >= Render's 15-minute idle timeout; "
            "the service may still spin down.",
            settings.interval,
        )

    runner: web.AppRunner | None = None
    ping_task: asyncio.Task | None = None

    if settings.should_serve:
        try:
            runner = web.AppRunner(_build_app())
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", settings.port)
            await site.start()
            log.info("Keep-alive HTTP server listening on 0.0.0.0:%s", settings.port)
        except OSError as exc:
            log.error("Keep-alive failed to bind 0.0.0.0:%s: %s", settings.port, exc)
            if runner is not None:
                await runner.cleanup()
            runner = None

    if settings.should_ping:
        ping_task = asyncio.create_task(
            ping_loop(settings.urls, settings.interval),
            name="keepalive-ping",
        )
        log.info(
            "Keep-alive pinger targeting %s every %ss",
            ", ".join(settings.urls),
            settings.interval,
        )

    try:
        if ping_task is not None:
            await ping_task
        else:
            await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
    finally:
        if ping_task is not None and not ping_task.done():
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        if runner is not None:
            await runner.cleanup()


async def start_keepalive_background(
    settings: KeepAliveSettings | None = None,
) -> asyncio.Task | None:
    """
    Start keep-alive once per process.

    Safe to call from both run.py (early, so Render sees $PORT bound) and
    bot.py post_init (standalone `python -m renewise.bot`). The second call
    is a no-op.
    """
    global _started
    if _started:
        return None

    settings = settings or load_settings()
    if not settings.should_run:
        log.info("Keep-alive idle — disabled or no PORT / URLs configured.")
        return None

    _started = True
    task = asyncio.create_task(run_keepalive(settings), name="keepalive")
    log.info("Keep-alive task started.")
    return task


def reset_started_for_tests() -> None:
    """Test helper — do not use in production code."""
    global _started
    _started = False


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    settings = load_settings()
    if not settings.should_run:
        log.error(
            "Nothing to do. Set PORT / RENDER_EXTERNAL_URL (Render), "
            "or KEEP_ALIVE_URL / KEEP_ALIVE_URLS."
        )
        sys.exit(1)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run_keepalive(settings))
    except KeyboardInterrupt:
        log.info("Keep-alive stopped.")


if __name__ == "__main__":
    main()
