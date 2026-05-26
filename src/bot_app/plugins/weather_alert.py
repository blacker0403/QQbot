from __future__ import annotations

import asyncio
import logging

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Bot

from bot_app.runtime import get_runtime
from bot_app.services.weather_alert import (
    OpenMeteoWeatherClient,
    evaluate_weather_alerts,
    is_weather_alert_quiet_time,
)

logger = logging.getLogger(__name__)
driver = get_driver()
_tasks: dict[str, asyncio.Task] = {}


@driver.on_bot_connect
async def start_weather_alert_loop(bot: Bot) -> None:
    runtime = get_runtime()
    config = runtime.config.weather_alert
    if not config.enabled or await runtime.store.is_weather_alert_paused():
        return

    bot_id = str(getattr(bot, "self_id", "default"))
    existing = _tasks.get(bot_id)
    if existing is not None and not existing.done():
        return

    _tasks[bot_id] = asyncio.create_task(_run_weather_alert_loop(bot))
    logger.info("Started weather alert loop for bot %s", bot_id)


@driver.on_bot_disconnect
async def stop_weather_alert_loop(bot: Bot) -> None:
    bot_id = str(getattr(bot, "self_id", "default"))
    task = _tasks.pop(bot_id, None)
    if task is not None:
        task.cancel()


@driver.on_shutdown
async def stop_all_weather_alert_tasks() -> None:
    for task in _tasks.values():
        task.cancel()
    _tasks.clear()


async def _run_weather_alert_loop(bot: Bot) -> None:
    runtime = get_runtime()
    config = runtime.config.weather_alert
    client = OpenMeteoWeatherClient(config)
    if config.initial_delay_seconds:
        await asyncio.sleep(config.initial_delay_seconds)

    while True:
        try:
            if await runtime.store.is_weather_alert_paused():
                await asyncio.sleep(max(60.0, config.check_interval_minutes * 60))
                continue
            if is_weather_alert_quiet_time():
                await asyncio.sleep(max(60.0, config.check_interval_minutes * 60))
                continue
            hours = await client.fetch_hourly()
            for alert in evaluate_weather_alerts(hours, config):
                should_send = await runtime.store.claim_weather_alert_key(alert.key)
                if not should_send:
                    continue
                await runtime.notifier.send_text(
                    bot,
                    alert.message,
                    recipients=[runtime.config.owner_qq],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Weather alert check failed: %r", exc)

        await asyncio.sleep(max(60.0, config.check_interval_minutes * 60))
