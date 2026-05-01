from __future__ import annotations

from datetime import datetime
import logging
import re

from bot_app.models import ExchangeParseResult, ResolvedSlot
from bot_app.services.learned_rules import match_rule
from bot_app.services.minimax import MiniMaxProvider
from bot_app.services.slot_parser import SlotParser

logger = logging.getLogger(__name__)

WANT_HAVE_RE = re.compile(r"(?:求|收)\s*(?P<want>.+?)[，,;；。\s]+\s*(?:我)?有\s*(?P<have>.+)")
HAVE_WANT_RE = re.compile(r"(?:(?:我)?有\s*)?(?P<have>.+?)\s*(?:想换|要换|换到|想要|需要)\s*(?P<want>.+)")
DIRECT_SWAP_RE = re.compile(r"(?:(?:我)?有\s*)?(?P<have>.+?)\s*换\s*(?P<want>.+)")


class ExchangeParseService:
    def __init__(self, slot_parser: SlotParser, minimax: MiniMaxProvider | None) -> None:
        self.slot_parser = slot_parser
        self.minimax = minimax

    @staticmethod
    def looks_like_exchange(text: str) -> bool:
        compact = text.strip()
        if any(marker in compact for marker in ("想换", "要换", "换到", "换")):
            return True
        return ("求" in compact or "收" in compact) and "有" in compact

    async def parse(self, text: str, reference_time: datetime) -> ExchangeParseResult:
        ruled = self._parse_by_rule(text, reference_time)
        if not ruled.needs_llm:
            return ruled
        if self.minimax is None:
            return ruled.model_copy(
                update={
                    "is_exchange_candidate": False,
                    "confidence": 0.0,
                    "reason": f"{ruled.reason}；缺少 MiniMax API Key，未放行",
                    "needs_llm": False,
                }
            )
        try:
            return await self.minimax.assess_exchange(
                text=text,
                reference_time=reference_time,
                target_aliases=self.slot_parser.config.target_campus_aliases,
            )
        except Exception as exc:
            logger.warning("MiniMax exchange assessment failed: %r", exc)
            return ruled.model_copy(
                update={
                    "is_exchange_candidate": False,
                    "confidence": 0.0,
                    "reason": f"{ruled.reason}；MiniMax 调用失败，保守拒绝",
                    "needs_llm": False,
                }
            )

    def _parse_by_rule(self, text: str, reference_time: datetime) -> ExchangeParseResult:
        learned_rule = match_rule(self.slot_parser.config.self_learning.learned_rules_path, text, {"exchange"})
        if learned_rule is not None:
            their_have_slots = [
                ResolvedSlot.model_validate(slot)
                for slot in learned_rule.get("their_have_slots", [])
                if isinstance(slot, dict)
            ]
            their_want_slots = [
                ResolvedSlot.model_validate(slot)
                for slot in learned_rule.get("their_want_slots", [])
                if isinstance(slot, dict)
            ]
            if not their_have_slots or not their_want_slots:
                have_text = str(learned_rule.get("have_text", ""))
                want_text = str(learned_rule.get("want_text", ""))
                their_have_slots = self.slot_parser.parse_slot_list(have_text, reference_time)
                fallback_date = their_have_slots[-1].date if their_have_slots else None
                fallback_campus = their_have_slots[-1].campus if their_have_slots else None
                their_want_slots = self.slot_parser.parse_swap_rule_list(
                    want_text,
                    reference_time,
                    fallback_date=fallback_date,
                    fallback_campus=fallback_campus,
                )
            if their_have_slots and their_want_slots:
                confidence = learned_rule.get("confidence", 0.97)
                return ExchangeParseResult(
                    is_exchange_candidate=True,
                    their_have_slots=their_have_slots,
                    their_want_slots=their_want_slots,
                    confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.97,
                    reason=f"自学习规则命中：{learned_rule.get('rule_id', 'unknown')}",
                    needs_llm=False,
                )

        if not self.looks_like_exchange(text):
            return ExchangeParseResult(
                is_exchange_candidate=False,
                reason="未命中换场关键词",
                needs_llm=False,
            )

        sides = self._split_sides(text)
        if sides is None:
            return ExchangeParseResult(
                is_exchange_candidate=False,
                confidence=0.2,
                reason="命中换场关键词，但规则解析未能确定双方槽位",
                needs_llm=True,
            )

        have_text, want_text = sides
        their_have_slots = self.slot_parser.parse_slot_list(have_text, reference_time)
        fallback_date = their_have_slots[-1].date if their_have_slots else None
        fallback_campus = their_have_slots[-1].campus if their_have_slots else None
        their_want_slots = self.slot_parser.parse_slot_list(
            want_text,
            reference_time,
            fallback_date=fallback_date,
            fallback_campus=fallback_campus,
        )

        if their_have_slots and their_want_slots:
            return ExchangeParseResult(
                is_exchange_candidate=True,
                their_have_slots=their_have_slots,
                their_want_slots=their_want_slots,
                confidence=0.95,
                reason="规则命中换场语义",
                needs_llm=False,
            )

        return ExchangeParseResult(
            is_exchange_candidate=False,
            their_have_slots=their_have_slots,
            their_want_slots=their_want_slots,
            confidence=0.3,
            reason="换场消息已识别，但槽位信息不完整",
            needs_llm=True,
        )

    @staticmethod
    def _split_sides(text: str) -> tuple[str, str] | None:
        compact = text.strip()
        if match := WANT_HAVE_RE.search(compact):
            return match.group("have").strip(), match.group("want").strip()

        if match := HAVE_WANT_RE.search(compact):
            return match.group("have").strip(), match.group("want").strip()

        if match := DIRECT_SWAP_RE.search(compact):
            have_text = match.group("have").strip()
            want_text = match.group("want").strip()
            if have_text and want_text:
                return have_text, want_text
        return None

    @staticmethod
    def slot_matches(left: ResolvedSlot, right: ResolvedSlot) -> bool:
        if left.date != right.date or left.campus != right.campus:
            return False
        if left.match_mode == "start_after":
            return right.start_time >= left.start_time
        if right.match_mode == "start_after":
            return left.start_time >= right.start_time
        return left.start_time == right.start_time and left.end_time == right.end_time
