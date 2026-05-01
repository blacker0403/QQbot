from __future__ import annotations

import logging
from datetime import timedelta

from nonebot.adapters.onebot.v11 import Bot

from bot_app.config import AppConfig
from bot_app.models import ApprovalTask, ApprovalTaskKind, AutoRecallTask, IncomingGroupMessage, ParsedCandidate, ResolvedSlot, SwapWatchRule
from bot_app.storage import JsonStateStore

logger = logging.getLogger(__name__)


def _ob_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _format_remaining(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}小时{minutes}分钟"


def format_slot(slot: ResolvedSlot | None) -> str:
    if slot is None:
        return "未知"
    if slot.match_mode == "start_after":
        return f"{slot.date} {slot.start_time}以后 {slot.campus}"
    return f"{slot.date} {slot.start_time}-{slot.end_time} {slot.campus}"


class NotifyService:
    def __init__(self, config: AppConfig, store: JsonStateStore) -> None:
        self.config = config
        self.store = store

    async def _owner_recipients(self) -> list[str]:
        recipients = [self.config.owner_qq]
        secondary_owner_qq = await self.store.get_secondary_owner_qq()
        if not secondary_owner_qq:
            secondary_owner_qq = self.config.secondary_owner_qq
        if secondary_owner_qq and secondary_owner_qq not in recipients:
            recipients.append(secondary_owner_qq)
        return recipients

    async def send_text(self, bot: Bot, message: str, recipients: list[str] | None = None) -> None:
        target_recipients = recipients or await self._owner_recipients()
        sent_count = 0
        last_error: Exception | None = None
        failed_recipients: list[tuple[str, Exception]] = []
        for recipient in target_recipients:
            try:
                await bot.send_private_msg(user_id=_ob_id(recipient), message=message)
                sent_count += 1
            except Exception as exc:
                last_error = exc
                failed_recipients.append((recipient, exc))
                logger.warning("Failed to send private message to %s: %s", recipient, exc)
        await self._notify_owner_about_delivery_failures(bot, failed_recipients)
        if sent_count == 0 and last_error is not None:
            raise last_error

    async def _notify_owner_about_delivery_failures(
        self,
        bot: Bot,
        failed_recipients: list[tuple[str, Exception]],
    ) -> None:
        owner_qq = self.config.owner_qq
        for recipient, exc in failed_recipients:
            if recipient == owner_qq:
                continue
            try:
                await bot.send_private_msg(
                    user_id=_ob_id(owner_qq),
                    message=f"提醒：给 {recipient} 发送私信失败：{exc}",
                )
            except Exception as notify_exc:
                logger.warning("Failed to send delivery warning to %s: %s", owner_qq, notify_exc)

    @staticmethod
    def _format_time_range(start_time: str | None, end_time: str | None, slot_date: str | None = None) -> str:
        prefix = f"{slot_date} " if slot_date else "今天"
        if start_time and end_time:
            return f"{prefix}{start_time}-{end_time}"
        if start_time:
            return f"{prefix}{start_time}"
        return "未知"

    async def send_candidate_notice(
        self,
        bot: Bot,
        incoming: IncomingGroupMessage,
        parsed: ParsedCandidate,
        task: ApprovalTask,
    ) -> None:
        await self.send_text(
            bot,
            "\n".join(
                [
                    "【候选场地】",
                    f"群聊：{task.group_name} ({task.group_id})",
                    f"发送人：{incoming.nickname} ({incoming.user_id})",
                    f"时间：{self._format_time_range(task.start_time, task.end_time, task.slot_date)}",
                    f"原文：{incoming.raw_text}",
                    f"确认码：{task.token}",
                    f"过期：{task.expires_at.strftime('%Y-%m-%d %H:%M:%S')}",
                    "回复 1 确认，回复 0 取消",
                ]
            ),
        )

    async def send_swap_match_notice(
        self,
        bot: Bot,
        incoming: IncomingGroupMessage,
        task: ApprovalTask,
        rule: SwapWatchRule,
    ) -> None:
        await self.send_text(
            bot,
            "\n".join(
                [
                    "【换场匹配命中】",
                    f"规则：{rule.name} ({rule.rule_id})",
                    f"我可提供：{format_slot(task.matched_have_slot)}",
                    f"我想换到：{format_slot(task.matched_want_slot)}",
                    f"群聊：{task.group_name} ({task.group_id})",
                    f"发送人：{incoming.nickname} ({incoming.user_id})",
                    f"原文：{incoming.raw_text}",
                    f"确认码：{task.token}",
                    f"过期：{task.expires_at.strftime('%Y-%m-%d %H:%M:%S')}",
                    "回复 1 将引用原消息并 @对方 后发送 1，回复 0 取消",
                ]
            ),
        )

    async def send_cooldown_notice(
        self,
        bot: Bot,
        incoming: IncomingGroupMessage,
        parsed: ParsedCandidate,
        group_name: str,
        remaining: timedelta,
        recipients: list[str] | None = None,
    ) -> None:
        await self.send_text(
            bot,
            "\n".join(
                [
                    "【命中但在冷却中】",
                    f"剩余冷却：{_format_remaining(remaining)}",
                    f"群聊：{group_name} ({incoming.group_id})",
                    f"发送人：{incoming.nickname} ({incoming.user_id})",
                    f"时间：{self._format_time_range(parsed.start_time.strftime('%H:%M') if parsed.start_time else None, parsed.end_time.strftime('%H:%M') if parsed.end_time else None)}",
                    f"原文：{incoming.raw_text}",
                ]
            ),
            recipients=recipients,
        )

    async def send_confirm_result(self, bot: Bot, task: ApprovalTask) -> None:
        lines = [
            "【已代发 1】" if task.task_kind == ApprovalTaskKind.CLAIM else "【已引用并艾特后发送 1】",
            f"群聊：{task.group_name} ({task.group_id})",
            f"发送人：{task.sender_nickname} ({task.user_id})",
            f"时间：{self._format_time_range(task.start_time, task.end_time, task.slot_date)}",
            f"原文：{task.raw_text}",
            f"发送时间：{task.sent_at.strftime('%Y-%m-%d %H:%M:%S') if task.sent_at else '未知'}",
        ]
        if task.task_kind == ApprovalTaskKind.SWAP_MATCH:
            lines.insert(1, f"规则：{task.matched_rule_id or '未知'}")
            lines.insert(2, f"我可提供：{format_slot(task.matched_have_slot)}")
            lines.insert(3, f"我想换到：{format_slot(task.matched_want_slot)}")
        await self.send_text(
            bot,
            "\n".join(lines),
        )

    async def send_auto_claim_result(self, bot: Bot, task: ApprovalTask, remaining: timedelta) -> None:
        pending_auto_recall = await self.store.get_pending_auto_recall()
        recall_hint = (
            "如需撤回刚刚自动发送的 1，请直接回复 0"
            if pending_auto_recall and pending_auto_recall.task_id == task.task_id
            else "当前无法撤回这条自动发送的 1"
        )
        await self.send_text(
            bot,
            "\n".join(
                [
                    "【自动模式已扣 1】",
                    f"群聊：{task.group_name} ({task.group_id})",
                    f"发送人：{task.sender_nickname} ({task.user_id})",
                    f"时间：{self._format_time_range(task.start_time, task.end_time, task.slot_date)}",
                    f"原文：{task.raw_text}",
                    f"发送时间：{task.sent_at.strftime('%Y-%m-%d %H:%M:%S') if task.sent_at else '未知'}",
                    f"自动模式冷却：{_format_remaining(remaining)}",
                    recall_hint,
                ]
            ),
        )

    async def send_auto_recall_result(self, bot: Bot, recall_task: AutoRecallTask) -> None:
        await self.send_text(
            bot,
            "\n".join(
                [
                    "【已撤回自动发送的 1】",
                    f"群聊：{recall_task.group_name} ({recall_task.group_id})",
                    f"发送人：{recall_task.sender_nickname} ({recall_task.user_id})",
                    f"时间：{self._format_time_range(recall_task.start_time, recall_task.end_time, recall_task.slot_date)}",
                    f"原文：{recall_task.raw_text}",
                    f"原发送时间：{recall_task.sent_at.strftime('%Y-%m-%d %H:%M:%S')}",
                ]
            ),
            recipients=[self.config.owner_qq],
        )

    async def send_failure_result(
        self,
        bot: Bot,
        message: str,
        recipient: str | None = None,
    ) -> None:
        recipients = [recipient] if recipient else None
        await self.send_text(bot, message, recipients=recipients)
