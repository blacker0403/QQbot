from __future__ import annotations

import asyncio
import logging
import random

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Bot

from bot_app.runtime import get_runtime
from bot_app.services.avatar_rotation import AvatarRotator

logger = logging.getLogger(__name__)
driver = get_driver()
_tasks: dict[str, asyncio.Task] = {}


@driver.on_bot_connect
async def start_avatar_rotation(bot: Bot) -> None:
    runtime = get_runtime()
    config = runtime.config.avatar_rotation
    if not config.enabled:
        return

    bot_id = str(getattr(bot, "self_id", "default"))
    existing = _tasks.get(bot_id)
    if existing is not None and not existing.done():
        return

    _tasks[bot_id] = asyncio.create_task(_run_avatar_rotation_loop(bot))
    logger.info("Started avatar rotation loop for bot %s", bot_id)


@driver.on_bot_disconnect
async def stop_avatar_rotation(bot: Bot) -> None:
    bot_id = str(getattr(bot, "self_id", "default"))
    task = _tasks.pop(bot_id, None)
    if task is not None:
        task.cancel()


@driver.on_shutdown
async def stop_all_avatar_rotation_tasks() -> None:
    for task in _tasks.values():
        task.cancel()
    _tasks.clear()


async def _run_avatar_rotation_loop(bot: Bot) -> None:
    config = get_runtime().config.avatar_rotation
    rotator = AvatarRotator(config)
    if config.initial_delay_seconds:
        await asyncio.sleep(config.initial_delay_seconds)

    while True:
        try:
            await rotator.rotate_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Avatar rotation failed: %r", exc)

        interval_seconds = config.interval_hours * 3600
        jitter_seconds = config.random_jitter_minutes * 60
        delay = interval_seconds + random.uniform(-jitter_seconds, jitter_seconds)
        await asyncio.sleep(max(60.0, delay))
