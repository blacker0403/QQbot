from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging

from nonebot.adapters.onebot.v11 import Bot

from bot_app.config import AppConfig
from bot_app.models import ApprovalTaskKind, AutoRecallTask, ClaimMode, IncomingGroupMessage, ParsedCandidate, ResolvedSlot, SwapWatchRule
from bot_app.services.approval import ApprovalService
from bot_app.services.cooldown import CooldownService
from bot_app.services.exchange_parse import ExchangeParseService
from bot_app.services.notify import NotifyService
from bot_app.services.prefilter import PrefilterService
from bot_app.services.semantic_parse import SemanticParseService
from bot_app.services.slot_parser import SlotParser
from bot_app.storage import JsonStateStore

logger = logging.getLogger(__name__)


def _ob_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _extract_sent_message_id(result: object) -> str | None:
    if isinstance(result, dict):
        message_id = result.get("message_id")
        if message_id is None:
            return None
        return str(message_id)
    if isinstance(result, (int, str)):
        return str(result)
    return None


def _delete_message_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


class ClaimWorkflow:
    def __init__(
        self,
        config: AppConfig,
        store: JsonStateStore,
        prefilter: PrefilterService,
        slot_parser: SlotParser,
        exchange_parser: ExchangeParseService,
        parser: SemanticParseService,
        approval: ApprovalService,
        cooldown: CooldownService,
        notifier: NotifyService,
    ) -> None:
        self.config = config
        self.store = store
        self.prefilter = prefilter
        self.slot_parser = slot_parser
        self.exchange_parser = exchange_parser
        self.parser = parser
        self.approval = approval
        self.cooldown = cooldown
        self.notifier = notifier
        self._recent_claims_by_group: dict[str, list[tuple[datetime, str, str]]] = {}

    async def handle_group_message(self, bot: Bot, incoming: IncomingGroupMessage) -> None:
        await self.store.expire_overdue_tasks()

        if incoming.group_id not in self.config.target_groups:
            return

        if self._is_plain_claim(incoming.raw_text):
            self._remember_group_claim(bot, incoming)

        group_name: str | None = None

        if self.prefilter.match(incoming.raw_text):
            parsed = await self.parser.parse(incoming.raw_text)
            slot = self.slot_parser.parse_slot(incoming.raw_text, incoming.timestamp)
            temporary_slot = slot or self._slot_from_parsed(incoming, parsed)
            temporary_claim = await self._matches_temporary_claim_slot(incoming, parsed, temporary_slot)
            if await self.store.is_claim_listening_paused() and not temporary_claim:
                logger.info("Skipped claim listening for message %s because listening is paused", incoming.message_id)
            elif parsed.is_candidate or temporary_claim:
                slot_for_checks = (
                    None
                    if not temporary_claim and self._looks_like_question(incoming.raw_text)
                    else temporary_slot
                )
                if self._starts_within_claim_lead_time(
                    slot_for_checks,
                    incoming.timestamp,
                ) or (
                    slot_for_checks is None
                    and self._parsed_starts_within_claim_lead_time(parsed, incoming.timestamp, incoming.raw_text)
                ):
                    logger.info("Ignored claim message %s because slot starts within one hour", incoming.message_id)
                    return
                if not temporary_claim and await self.store.is_dismissed_claim_slot(temporary_slot, incoming.timestamp):
                    logger.info("Ignored claim message %s because the slot was dismissed today", incoming.message_id)
                    return
                if temporary_claim and not parsed.is_candidate:
                    parsed = self._promote_temporary_claim(parsed, temporary_slot)
                group_name = await self._fetch_group_name(bot, incoming.group_id)
                await self._handle_claim_candidate(
                    bot=bot,
                    incoming=incoming,
                    group_name=group_name,
                    parsed=parsed,
                    slot_date=slot_for_checks.date if slot_for_checks else None,
                    temporary_claim=temporary_claim,
                )
            else:
                logger.info("Ignored claim message %s: %s", incoming.message_id, parsed.reason)

        await self._handle_swap_watch(bot, incoming, group_name)

    @staticmethod
    def _starts_within_claim_lead_time(slot: ResolvedSlot | None, reference_time: datetime) -> bool:
        if slot is None:
            return False
        try:
            slot_start = datetime.fromisoformat(f"{slot.date}T{slot.start_time}")
        except ValueError:
            return False
        return slot_start - reference_time < timedelta(hours=1)

    @staticmethod
    def _has_explicit_future_date(text: str) -> bool:
        return any(marker in text for marker in ("明天", "明晚", "明日", "后天", "周", "星期"))

    @staticmethod
    def _parsed_starts_within_claim_lead_time(parsed: ParsedCandidate, reference_time: datetime, text: str) -> bool:
        if parsed.start_time is None:
            return False
        if ClaimWorkflow._has_explicit_future_date(text):
            return False
        slot_start = datetime.combine(reference_time.date(), parsed.start_time)
        return slot_start - reference_time < timedelta(hours=1)

    def _looks_like_question(self, text: str) -> bool:
        looks_like_question = getattr(self.parser, "_looks_like_question", None)
        if looks_like_question is None:
            return False
        return bool(looks_like_question(text))

    async def _matches_temporary_claim_slot(
        self,
        incoming: IncomingGroupMessage,
        parsed: ParsedCandidate,
        slot: ResolvedSlot | None,
    ) -> bool:
        if slot is None or slot.end_time is None:
            return False
        if parsed.start_time is None or parsed.end_time is None:
            return False
        if self.parser._detect_intent(incoming.raw_text) is False or self.parser._looks_like_question(incoming.raw_text):
            return False
        if not self.parser._has_offer_action(incoming.raw_text):
            return False
        for temporary_slot in await self.store.list_temporary_claim_slots(now=incoming.timestamp):
            if temporary_slot.date != slot.date:
                continue
            if temporary_slot.start_time != slot.start_time or temporary_slot.end_time != slot.end_time:
                continue
            if temporary_slot.campus and temporary_slot.campus != slot.campus:
                continue
            return True
        return False

    def _slot_from_parsed(
        self,
        incoming: IncomingGroupMessage,
        parsed: ParsedCandidate,
    ) -> ResolvedSlot | None:
        if parsed.start_time is None or parsed.end_time is None:
            return None
        campus = parsed.campus
        if campus is None:
            campus = self.config.target_campus_aliases[0] if self.config.target_campus_aliases else self.config.target_campus_key
        return ResolvedSlot(
            date=incoming.timestamp.date().isoformat(),
            start_time=parsed.start_time.strftime("%H:%M"),
            end_time=parsed.end_time.strftime("%H:%M"),
            campus=campus,
            raw_text=incoming.raw_text,
        )

    @staticmethod
    def _promote_temporary_claim(parsed: ParsedCandidate, slot: ResolvedSlot | None) -> ParsedCandidate:
        return parsed.model_copy(
            update={
                "is_candidate": True,
                "campus": slot.campus if slot else parsed.campus,
                "start_time": parsed.start_time,
                "end_time": parsed.end_time,
                "confidence": max(parsed.confidence, 0.97),
                "reason": "临时抢场规则命中",
                "needs_llm": False,
            }
        )

    async def _handle_claim_candidate(
        self,
        bot: Bot,
        incoming: IncomingGroupMessage,
        group_name: str,
        parsed,
        slot_date: str | None,
        temporary_claim: bool = False,
    ) -> None:
        mode = await self.store.get_claim_mode()
        manages_auto_state = mode == ClaimMode.AUTO and not temporary_claim
        if manages_auto_state:
            remaining = await self.cooldown.get_remaining(incoming.timestamp)
            if remaining.total_seconds() > 0:
                await self.notifier.send_cooldown_notice(
                    bot,
                    incoming,
                    parsed,
                    group_name,
                    remaining,
                    recipients=[self.config.owner_qq],
                )
                logger.info("Skipped auto claim for message %s because cooldown is active", incoming.message_id)
                return

        task = await self.approval.create_task(
            incoming,
            parsed,
            group_name,
            slot_date=slot_date,
        )
        logger.info("Created approval task %s for message %s", task.task_id, incoming.message_id)

        if mode == ClaimMode.MANUAL:
            await self.notifier.send_candidate_notice(bot, incoming, parsed, task)
            return

        if manages_auto_state:
            await self.cooldown.mark_claimed(incoming.timestamp)
        await asyncio.sleep(1)
        try:
            send_result = await bot.send_group_msg(group_id=_ob_id(task.group_id), message=task.reply_text)
        except Exception as exc:
            logger.exception("Failed to auto-send claim message for task %s", task.task_id)
            await self.approval.mark_failed(task, str(exc))
            if manages_auto_state:
                await self.cooldown.reset()
                await self.store.set_pending_auto_recall(None)
            await self.notifier.send_failure_result(
                bot,
                "\n".join(
                    [
                        "【自动扣 1 失败】",
                        f"群聊：{task.group_name} ({task.group_id})",
                        f"发送人：{task.sender_nickname} ({task.user_id})",
                        f"时间：{self.notifier._format_time_range(task.start_time, task.end_time, task.slot_date)}",
                        f"原文：{task.raw_text}",
                        f"原因：{exc}",
                    ]
                ),
                recipient=self.config.owner_qq,
            )
            return

        updated = await self.approval.mark_sent(task, datetime.now())
        sent_message_id = _extract_sent_message_id(send_result)
        if manages_auto_state and sent_message_id is not None:
            await self.store.set_pending_auto_recall(
                AutoRecallTask(
                    task_id=updated.task_id,
                    group_id=updated.group_id,
                    group_name=updated.group_name,
                    sent_message_id=sent_message_id,
                    user_id=updated.user_id,
                    sender_nickname=updated.sender_nickname,
                    raw_text=updated.raw_text,
                    campus=updated.campus,
                    slot_date=updated.slot_date,
                    start_time=updated.start_time,
                    end_time=updated.end_time,
                    sent_at=updated.sent_at or datetime.now(),
                )
            )
        elif manages_auto_state:
            await self.store.set_pending_auto_recall(None)
        if manages_auto_state and await self._recall_auto_claim_if_llm_rejects(bot, updated, sent_message_id, parsed):
            return
        await self.notifier.send_auto_claim_result(bot, updated, await self.cooldown.get_remaining(updated.sent_at))

    def has_later_group_claim(self, task) -> bool:
        claims = self._recent_claims_by_group.get(task.group_id, [])
        baseline = task.source_timestamp or task.created_at
        for claimed_at, user_id, _message_id in claims:
            if user_id == self.config.owner_qq:
                continue
            if claimed_at >= baseline:
                return True
        return False

    async def _recall_auto_claim_if_llm_rejects(self, bot: Bot, task, sent_message_id: str | None, parsed) -> bool:
        verdict = await self.parser.verify_offer_with_llm(task.raw_text, parsed)
        if verdict is not False:
            return False

        if sent_message_id is not None:
            try:
                await bot.delete_msg(message_id=_delete_message_id(sent_message_id))
            except Exception as exc:
                logger.warning("Failed to recall rejected auto claim %s: %s", task.task_id, exc)

        try:
            await bot.send_group_msg(group_id=_ob_id(task.group_id), message="抱歉，看错了")
        except Exception as exc:
            logger.warning("Failed to send auto-claim correction apology for %s: %s", task.task_id, exc)

        await self.approval.mark_failed(task, "MiniMax rejected auto claim after send")
        await self.cooldown.reset()
        await self.store.set_pending_auto_recall(None)
        await self.notifier.send_failure_result(
            bot,
            "\n".join(
                [
                    "【自动扣 1 已撤回】",
                    f"群聊：{task.group_name} ({task.group_id})",
                    f"发送人：{task.sender_nickname} ({task.user_id})",
                    f"原文：{task.raw_text}",
                    "原因：MiniMax 复核认为这不是有效送场消息",
                ]
            ),
            recipient=self.config.owner_qq,
        )
        return True

    def _remember_group_claim(self, bot: Bot, incoming: IncomingGroupMessage) -> None:
        bot_self_id = str(getattr(bot, "self_id", ""))
        if bot_self_id and incoming.user_id == bot_self_id:
            return
        claims = self._recent_claims_by_group.setdefault(incoming.group_id, [])
        cutoff = incoming.timestamp.timestamp() - self.config.approval_ttl_seconds
        claims[:] = [item for item in claims if item[0].timestamp() >= cutoff]
        claims.append((incoming.timestamp, incoming.user_id, incoming.message_id))

    @staticmethod
    def _is_plain_claim(text: str) -> bool:
        return text.strip() in {"1", "１"}

    async def _handle_swap_watch(
        self,
        bot: Bot,
        incoming: IncomingGroupMessage,
        group_name: str | None,
    ) -> None:
        rules = [rule for rule in await self.store.list_swap_watch_rules(now=incoming.timestamp) if rule.enabled]
        if not rules:
            return
        if not self.exchange_parser.looks_like_exchange(incoming.raw_text):
            return

        parsed = await self.exchange_parser.parse(incoming.raw_text, incoming.timestamp)
        if not parsed.is_exchange_candidate:
            logger.info("Ignored swap message %s: %s", incoming.message_id, parsed.reason)
            return

        resolved_group_name = group_name or await self._fetch_group_name(bot, incoming.group_id)
        seen_match_keys: set[tuple] = set()
        for rule in rules:
            existing_task = await self.store.find_task(
                task_kind=ApprovalTaskKind.SWAP_MATCH,
                message_id=incoming.message_id,
                matched_rule_id=rule.rule_id,
            )
            if existing_task is not None:
                continue
            matched_have_slot, matched_want_slot = self._find_swap_match(rule, parsed.their_have_slots, parsed.their_want_slots)
            if matched_have_slot is None or matched_want_slot is None:
                matched_have_slot, matched_want_slot = await self._find_swap_match_with_llm(
                    rule,
                    parsed.their_have_slots,
                    parsed.their_want_slots,
                )
            if matched_have_slot is None or matched_want_slot is None:
                continue
            match_key = (
                self._slot_signature(matched_have_slot),
                self._slot_signature(matched_want_slot),
            )
            if match_key in seen_match_keys:
                logger.info(
                    "Skipped duplicate swap match for message %s rule %s because an equivalent rule already matched",
                    incoming.message_id,
                    rule.rule_id,
                )
                continue
            seen_match_keys.add(match_key)

            task = await self.approval.create_swap_match_task(
                incoming=incoming,
                group_name=resolved_group_name,
                rule=rule,
                matched_have_slot=matched_have_slot,
                matched_want_slot=matched_want_slot,
                reason=f"换场规则命中：{rule.name}",
            )
            logger.info("Created swap match task %s for message %s rule %s", task.task_id, incoming.message_id, rule.rule_id)
            await self.notifier.send_swap_match_notice(bot, incoming, task, rule)

    def _find_swap_match(
        self,
        rule: SwapWatchRule,
        their_have_slots: list[ResolvedSlot],
        their_want_slots: list[ResolvedSlot],
    ) -> tuple[ResolvedSlot | None, ResolvedSlot | None]:
        matched_have_slot: ResolvedSlot | None = None
        matched_want_slot: ResolvedSlot | None = None
        for my_have_slot in rule.have_slots:
            if any(self.exchange_parser.slot_matches(my_have_slot, their_want_slot) for their_want_slot in their_want_slots):
                matched_have_slot = my_have_slot
                break
        for their_have_slot in their_have_slots:
            if any(self.exchange_parser.slot_matches(my_want_slot, their_have_slot) for my_want_slot in rule.want_slots):
                matched_want_slot = their_have_slot
                break
        return matched_have_slot, matched_want_slot

    async def _find_swap_match_with_llm(
        self,
        rule: SwapWatchRule,
        their_have_slots: list[ResolvedSlot],
        their_want_slots: list[ResolvedSlot],
    ) -> tuple[ResolvedSlot | None, ResolvedSlot | None]:
        minimax = self.exchange_parser.minimax
        if minimax is None:
            return None, None
        try:
            matched_have_index, matched_want_index = await minimax.assess_swap_match(
                rule=rule,
                their_have_slots=their_have_slots,
                their_want_slots=their_want_slots,
            )
        except Exception as exc:
            logger.warning("MiniMax swap match assessment failed: %r", exc)
            return None, None
        if matched_have_index is None or matched_want_index is None:
            return None, None
        if not (0 <= matched_have_index < len(rule.have_slots)):
            return None, None
        if not (0 <= matched_want_index < len(their_have_slots)):
            return None, None
        matched_have_slot = rule.have_slots[matched_have_index]
        matched_want_slot = their_have_slots[matched_want_index]
        if not any(self.exchange_parser.slot_matches(matched_have_slot, their_want_slot) for their_want_slot in their_want_slots):
            logger.info("Rejected MiniMax swap match because my have slot does not match their want slots")
            return None, None
        if not any(self.exchange_parser.slot_matches(my_want_slot, matched_want_slot) for my_want_slot in rule.want_slots):
            logger.info("Rejected MiniMax swap match because their have slot does not match my want slots")
            return None, None
        return matched_have_slot, matched_want_slot

    @staticmethod
    def _slot_signature(slot: ResolvedSlot) -> tuple:
        return (
            slot.date,
            slot.start_time,
            slot.end_time,
            slot.campus,
            slot.match_mode,
        )

    async def _fetch_group_name(self, bot: Bot, group_id: str) -> str:
        try:
            group_info = await bot.get_group_info(group_id=_ob_id(group_id), no_cache=True)
            group_name = group_info.get("group_name")
            if isinstance(group_name, str) and group_name.strip():
                return group_name
        except Exception as exc:
            logger.warning("Failed to fetch group name for %s: %s", group_id, exc)
        return f"群聊 {group_id}"
