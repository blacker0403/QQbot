from __future__ import annotations

from datetime import datetime
import logging

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from bot_app.models import IncomingGroupMessage
from bot_app.runtime import get_runtime

logger = logging.getLogger(__name__)

group_message_matcher = on_message(priority=50, block=False)


@group_message_matcher.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        return

    runtime = get_runtime()
    incoming = IncomingGroupMessage(
        group_id=str(event.group_id),
        user_id=str(event.user_id),
        message_id=str(event.message_id),
        nickname=event.sender.card or event.sender.nickname or str(event.user_id),
        raw_text=event.get_plaintext().strip(),
        timestamp=datetime.fromtimestamp(event.time),
    )
    if not incoming.raw_text:
        return
    if incoming.raw_text.startswith("/"):
        logger.info("Skipped group command message %s in %s", incoming.message_id, incoming.group_id)
        return

    logger.info("Received group message %s in %s", incoming.message_id, incoming.group_id)
    await runtime.workflow.handle_group_message(bot, incoming)
    try:
        await runtime.self_learning.observe_group_message(incoming, bot_self_id=str(getattr(bot, "self_id", "")))
    except Exception as exc:
        logger.warning("Self learning failed for group message %s: %r", incoming.message_id, exc)
