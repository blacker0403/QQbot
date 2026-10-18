from __future__ import annotations

import logging

from nonebot import get_driver, on_message, on_notice, on_request
from nonebot.adapters.onebot.v11 import Bot, FriendAddNoticeEvent, FriendRequestEvent, PrivateMessageEvent

from bot_app.runtime import get_runtime

logger = logging.getLogger(__name__)
driver = get_driver()

friend_request_matcher = on_request(priority=5, block=False)
friend_add_matcher = on_notice(priority=5, block=False)
private_handoff_matcher = on_message(priority=5, block=False)


@friend_request_matcher.handle()
async def handle_friend_request(bot: Bot, event: FriendRequestEvent) -> None:
    if not isinstance(event, FriendRequestEvent):
        return
    handled = await get_runtime().court_handoff.handle_friend_request(
        bot,
        user_id=str(event.user_id),
        flag=event.flag,
    )
    if handled:
        logger.info("Approved expected court handoff friend request")


@friend_add_matcher.handle()
async def handle_friend_added(bot: Bot, event: FriendAddNoticeEvent) -> None:
    if not isinstance(event, FriendAddNoticeEvent):
        return
    await get_runtime().court_handoff.handle_friend_added(bot, str(event.user_id))


@private_handoff_matcher.handle()
async def handle_handoff_private_message(bot: Bot, event: PrivateMessageEvent) -> None:
    if not isinstance(event, PrivateMessageEvent):
        return
    image_candidates: list[list[str]] = []
    for segment in event.message:
        if segment.type != "image":
            continue
        candidates = [
            str(segment.data[key])
            for key in ("file", "url")
            if segment.data.get(key)
        ]
        if candidates:
            image_candidates.append(list(dict.fromkeys(candidates)))
    await get_runtime().court_handoff.handle_private_message(
        bot,
        user_id=str(event.user_id),
        text=event.get_plaintext(),
        image_candidates=image_candidates,
    )


@driver.on_bot_connect
async def resume_court_handoff(bot: Bot) -> None:
    try:
        await get_runtime().court_handoff.resume(bot)
    except Exception as exc:
        logger.warning("Court handoff resume failed: %r", exc)
