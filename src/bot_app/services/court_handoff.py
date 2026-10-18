from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import re
from typing import Any

from nonebot.adapters.onebot.v11 import Bot

from bot_app.config import AppConfig
from bot_app.models import (
    ApprovalTask,
    ApprovalTaskKind,
    ApprovalTaskStatus,
    CourtHandoff,
    CourtHandoffDetails,
    CourtHandoffStatus,
)
from bot_app.services.minimax import MiniMaxProvider
from bot_app.storage import JsonStateStore

logger = logging.getLogger(__name__)


def _ob_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


class CourtHandoffService:
    def __init__(
        self,
        config: AppConfig,
        store: JsonStateStore,
        minimax: MiniMaxProvider | None,
    ) -> None:
        self.config = config
        self.store = store
        self.minimax = minimax
        self._lock = asyncio.Lock()

    async def start(self, bot: Bot, task: ApprovalTask) -> None:
        if task.task_kind != ApprovalTaskKind.CLAIM or task.status != ApprovalTaskStatus.SENT:
            return
        if task.user_id == self.config.owner_qq or task.user_id == str(getattr(bot, "self_id", "")):
            return

        handoff = CourtHandoff(
            task_id=task.task_id,
            peer_qq=task.user_id,
            slot_date=task.slot_date,
            start_time=task.start_time,
            end_time=task.end_time,
            created_at=datetime.now(),
        )
        current, created = await self.store.start_court_handoff(handoff)
        if not created and current.task_id != task.task_id:
            logger.warning("Skipped court handoff %s because %s is still active", task.task_id, current.task_id)
            return
        try:
            await self.resume(bot)
        except Exception as exc:
            logger.warning("Court handoff %s could not check friendship yet: %r", task.task_id, exc)

    async def resume(self, bot: Bot) -> None:
        async with self._lock:
            handoff = await self.store.get_court_handoff()
            if handoff is None or handoff.status == CourtHandoffStatus.COMPLETED:
                return
            if handoff.status == CourtHandoffStatus.WAITING_FRIEND:
                if not await self._is_friend(bot, handoff.peer_qq):
                    return
                handoff = await self._send_greeting(bot, handoff)
            if handoff.status == CourtHandoffStatus.WAITING_DETAILS:
                await self._finish_if_complete(bot, handoff)

    async def handle_friend_request(self, bot: Bot, user_id: str, flag: str) -> bool:
        async with self._lock:
            handoff = await self.store.get_court_handoff()
            if (
                handoff is None
                or handoff.status != CourtHandoffStatus.WAITING_FRIEND
                or handoff.peer_qq != user_id
            ):
                return False
            await bot.set_friend_add_request(flag=flag, approve=True)
            await self._send_greeting(bot, handoff)
            return True

    async def handle_friend_added(self, bot: Bot, user_id: str) -> bool:
        async with self._lock:
            handoff = await self.store.get_court_handoff()
            if (
                handoff is None
                or handoff.status != CourtHandoffStatus.WAITING_FRIEND
                or handoff.peer_qq != user_id
            ):
                return False
            await self._send_greeting(bot, handoff)
            return True

    async def handle_private_message(
        self,
        bot: Bot,
        user_id: str,
        text: str,
        image_candidates: list[list[str]],
    ) -> bool:
        async with self._lock:
            handoff = await self.store.get_court_handoff()
            if (
                handoff is None
                or handoff.status != CourtHandoffStatus.WAITING_DETAILS
                or handoff.peer_qq != user_id
            ):
                return False

            new_texts: list[str] = []
            if text.strip():
                new_texts.append(text.strip())
            for candidates in image_candidates:
                ocr_text = await self._ocr_image(bot, candidates)
                if ocr_text:
                    new_texts.append(ocr_text)
            if not new_texts:
                return True

            handoff.received_texts.extend(new_texts)
            details = await self._extract_details(handoff)
            handoff.location = details.location or handoff.location
            handoff.card_number = details.card_number or handoff.card_number
            await self.store.update_court_handoff(handoff)
            await self._finish_if_complete(bot, handoff)
            return True

    async def _send_greeting(self, bot: Bot, handoff: CourtHandoff) -> CourtHandoff:
        if handoff.greeted_at is not None:
            return handoff
        await bot.send_private_msg(user_id=_ob_id(handoff.peer_qq), message="同学你好")
        updated = handoff.model_copy(
            update={
                "status": CourtHandoffStatus.WAITING_DETAILS,
                "greeted_at": datetime.now(),
            }
        )
        await self.store.update_court_handoff(updated)
        return updated

    async def _finish_if_complete(self, bot: Bot, handoff: CourtHandoff) -> None:
        if not handoff.location or not handoff.card_number:
            return
        if handoff.thanked_at is None:
            await bot.send_private_msg(user_id=_ob_id(handoff.peer_qq), message="谢谢")
            handoff = handoff.model_copy(update={"thanked_at": datetime.now()})
            await self.store.update_court_handoff(handoff)
        if handoff.owner_notified_at is None:
            await bot.send_private_msg(
                user_id=_ob_id(self.config.owner_qq),
                message=f"{self._format_slot_time(handoff)}-{handoff.location}-{handoff.card_number}",
            )
            handoff = handoff.model_copy(
                update={
                    "status": CourtHandoffStatus.COMPLETED,
                    "owner_notified_at": datetime.now(),
                }
            )
            await self.store.update_court_handoff(handoff)

    async def _is_friend(self, bot: Bot, user_id: str) -> bool:
        friends = await bot.get_friend_list()
        if isinstance(friends, dict):
            friends = friends.get("data", [])
        return any(str(friend.get("user_id")) == user_id for friend in friends if isinstance(friend, dict))

    async def _ocr_image(self, bot: Bot, candidates: list[str]) -> str:
        for image in candidates:
            try:
                result = await bot.call_api("ocr_image", image=image)
            except Exception as exc:
                logger.warning("Court handoff image OCR failed: %r", exc)
                continue
            text_parts = self._collect_ocr_text(result)
            if text_parts:
                return "\n".join(dict.fromkeys(text_parts))
        return ""

    @classmethod
    def _collect_ocr_text(cls, value: Any, key: str | None = None) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if key in {"text", "words", "content"} and value.strip() else []
        if isinstance(value, list):
            result: list[str] = []
            for item in value:
                result.extend(cls._collect_ocr_text(item, key))
            return result
        if isinstance(value, dict):
            result = []
            for child_key, child_value in value.items():
                result.extend(cls._collect_ocr_text(child_value, str(child_key).lower()))
            return result
        return []

    async def _extract_details(self, handoff: CourtHandoff) -> CourtHandoffDetails:
        combined_text = "\n".join(handoff.received_texts)
        if self.minimax is not None:
            try:
                details = await self.minimax.extract_court_handoff_details(
                    combined_text,
                    current_location=handoff.location,
                    current_card_number=handoff.card_number,
                )
                return self._normalize_details(details)
            except Exception as exc:
                logger.warning("MiniMax court handoff extraction failed: %r", exc)
        return self._fallback_extract_details(combined_text, handoff)

    @staticmethod
    def _normalize_details(details: CourtHandoffDetails) -> CourtHandoffDetails:
        location = details.location.strip(" \t\r\n，,。；;") if details.location else None
        if location in {"无", "未知", "null", "None"}:
            location = None
        card_number = re.sub(r"[^0-9A-Za-z]", "", details.card_number or "") or None
        return CourtHandoffDetails(location=location, card_number=card_number)

    @classmethod
    def _fallback_extract_details(
        cls,
        text: str,
        handoff: CourtHandoff,
    ) -> CourtHandoffDetails:
        card_number = handoff.card_number
        if not card_number:
            labeled_card = re.search(r"(?:一卡通(?:号)?|卡号)\s*[:：]?\s*([A-Za-z]?\d{6,20})", text)
            if labeled_card:
                card_number = labeled_card.group(1)
            else:
                standalone_cards = re.findall(r"(?<!\d)(\d{8,15})(?!\d)", text)
                if len(standalone_cards) == 1:
                    card_number = standalone_cards[0]

        location = handoff.location
        if not location:
            labeled_location = re.search(
                r"(?:位置|地点|场地)\s*[:：]?\s*([^\n，,；;]+)",
                text,
            )
            if labeled_location:
                location = labeled_location.group(1)
                location = re.split(r"(?:一卡通(?:号)?|卡号)", location)[0]
            elif card_number:
                for line in text.splitlines():
                    candidate = line.replace(card_number, "")
                    candidate = re.sub(r"(?:一卡通(?:号)?|卡号)\s*[:：]?", "", candidate)
                    candidate = candidate.strip(" \t，,。；;:-")
                    if re.search(r"[\u4e00-\u9fff]", candidate) and candidate not in {"同学你好", "谢谢"}:
                        location = candidate
                        break

        return cls._normalize_details(
            CourtHandoffDetails(location=location, card_number=card_number)
        )

    @staticmethod
    def _format_slot_time(handoff: CourtHandoff) -> str:
        date_text = handoff.slot_date or "日期未知"
        if handoff.start_time and handoff.end_time:
            return f"{date_text} {handoff.start_time}至{handoff.end_time}"
        if handoff.start_time:
            return f"{date_text} {handoff.start_time}"
        return date_text
