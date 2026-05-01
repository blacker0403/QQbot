from __future__ import annotations

from datetime import time
import logging
import re

from bot_app.config import AppConfig
from bot_app.models import ParsedCandidate
from bot_app.services.learned_rules import load_rules, match_rule
from bot_app.services.minimax import MiniMaxProvider

logger = logging.getLogger(__name__)

CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

STRONG_OFFER_PATTERNS = [
    r"送.{0,4}场",
    r"送.{0,4}片",
    r"出.{0,4}场",
    r"出.{0,4}片",
    r"转.{0,4}场",
    r"转.{0,4}片",
    r"场地.{0,3}出",
    r"场地.{0,3}送",
]

NEGATIVE_PATTERNS = [
    r"有没有人送",
    r"有没有送",
    r"有人送吗",
    r"有送吗",
    r"有.?人.*送.*吗",
    r"有.?人.*出.*吗",
    r"有.?人.*转.*吗",
    r"有没有.*送.*吗",
    r"有没有.*出.*吗",
    r"有没有.*转.*吗",
    r"想出吗",
    r"我想出门",
    r"出发",
    r"出票",
    r"出题",
    r"送水",
]

OFFER_ACTION_RE = re.compile(r"(送|出|转)")
QUESTION_MARKERS = ("吗", "？", "?", "求", "收", "问下")
MORNING_MARKERS = ("早上", "上午", "清晨")
EVENING_MARKERS = ("下午", "今晚", "明晚", "晚上", "傍晚", "晚")

COMPACT_RANGE_RE = re.compile(r"(?<!\d)(?P<start>[1-9])(?P<end>[1-9])(?!\d)")
MIXED_COMPACT_RANGE_RE = re.compile(
    r"(?P<start>[1-9一二两三四五六七八九])(?P<end>[1-9一二两三四五六七八九])(?![0-9零〇一二两三四五六七八九十])"
)
ARABIC_RANGE_RE = re.compile(
    r"(?P<hour>\d{1,2})(?:[:：](?P<minute>\d{1,2}))?\s*(?:点|时)?\s*[-~—到至]\s*(?P<end>\d{1,2})(?:[:：](?P<end_minute>\d{1,2}))?"
)
ARABIC_POINT_RE = re.compile(
    r"(?P<hour>\d{1,2})(?:[:：](?P<minute>\d{1,2}))?\s*(?:点|时)(?P<half>半)?"
)
CHINESE_RANGE_RE = re.compile(
    r"(?P<hour>[零〇一二两三四五六七八九十]+)点(?P<half>半)?\s*[-~—到至]\s*(?P<end>[零〇一二两三四五六七八九十]+)点?"
)
CHINESE_SIMPLE_RANGE_RE = re.compile(r"(?P<start>[一二两三四五六七八九])\s*[-~—到至]\s*(?P<end>[一二两三四五六七八九])")
CHINESE_COMPACT_RANGE_RE = re.compile(r"(?P<start>[一二两三四五六七八九])(?P<end>[一二两三四五六七八九])(?![零〇一二两三四五六七八九十])")
CHINESE_POINT_RE = re.compile(r"(?P<hour>[零〇一二两三四五六七八九十]+)点(?P<half>半)?")


def _parse_chinese_number(text: str) -> int:
    if text == "十":
        return 10
    if len(text) == 1:
        return CHINESE_DIGITS[text]
    if text.startswith("十"):
        return 10 + CHINESE_DIGITS[text[1]]
    if text.endswith("十"):
        return CHINESE_DIGITS[text[0]] * 10
    if "十" in text:
        left, right = text.split("十", maxsplit=1)
        return CHINESE_DIGITS[left] * 10 + CHINESE_DIGITS[right]
    raise ValueError(f"Unsupported Chinese number: {text}")


def _parse_compact_hour(token: str) -> int:
    if token.isdigit():
        return int(token)
    return _parse_chinese_number(token)


def _adjust_hour(hour: int, text: str) -> int:
    if hour >= 12:
        return hour
    if any(marker in text for marker in MORNING_MARKERS):
        return hour
    if any(marker in text for marker in EVENING_MARKERS):
        return hour + 12
    if hour == 11:
        return hour
    if 1 <= hour <= 10:
        return hour + 12
    return hour


def _build_time(hour: int, minute: int, text: str) -> time | None:
    hour = _adjust_hour(hour, text)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return time(hour=hour, minute=minute)


def _build_short_range(start_hour: int, end_hour: int, text: str) -> tuple[time | None, time | None]:
    if not (1 <= start_hour <= 12 and 1 <= end_hour <= 12):
        return None, None
    if start_hour >= end_hour:
        return None, None
    start_hour, end_hour = _adjust_range_hours(start_hour, end_hour, text)
    return time(hour=start_hour, minute=0), time(hour=end_hour, minute=0)


def _adjust_range_hours(start_hour: int, end_hour: int, text: str) -> tuple[int, int]:
    if any(marker in text for marker in MORNING_MARKERS):
        return start_hour, end_hour
    if any(marker in text for marker in EVENING_MARKERS):
        return (
            start_hour if start_hour >= 12 else start_hour + 12,
            end_hour if end_hour >= 12 else end_hour + 12,
        )
    if 9 <= start_hour <= 11 and end_hour <= 12:
        return start_hour, end_hour
    return (
        start_hour if start_hour >= 12 else start_hour + 12,
        end_hour if end_hour >= 12 else end_hour + 12,
    )


class SemanticParseService:
    def __init__(self, config: AppConfig, minimax: MiniMaxProvider | None) -> None:
        self.config = config
        self.minimax = minimax

    async def parse(self, text: str) -> ParsedCandidate:
        ruled = self._parse_by_rule(text)
        if not ruled.needs_llm:
            return ruled
        if self.minimax is None:
            return ruled.model_copy(
                update={
                    "is_candidate": False,
                    "confidence": 0.0,
                    "reason": f"{ruled.reason}；缺少 MiniMax API Key，未放行",
                    "needs_llm": False,
                }
            )
        try:
            llm_decision = await self.minimax.assess_offer(
                text=text,
                target_aliases=self.config.target_campus_aliases,
                min_time=self.config.min_start_time_obj,
                local_rule_context=self._build_offer_review_context(ruled),
            )
        except Exception as exc:
            logger.warning("MiniMax assessment failed: %r", exc)
            return ruled.model_copy(
                update={
                    "is_candidate": False,
                    "confidence": 0.0,
                    "reason": f"{ruled.reason}；MiniMax 调用失败，保守拒绝",
                    "needs_llm": False,
                }
            )

        start_time = self._parse_time_string(llm_decision.start_time) if llm_decision.start_time else None
        is_target_campus = self._is_target_campus(llm_decision.campus)
        meets_time = start_time is not None and start_time >= self.config.min_start_time_obj
        if llm_decision.is_real_offer and is_target_campus and meets_time:
            return ParsedCandidate(
                is_candidate=True,
                campus=llm_decision.campus,
                start_time=start_time,
                end_time=None,
                confidence=llm_decision.confidence,
                reason=f"MiniMax 判定通过：{llm_decision.reason}",
                needs_llm=False,
            )
        return ParsedCandidate(
            is_candidate=False,
            campus=llm_decision.campus,
            start_time=start_time,
            end_time=None,
            confidence=llm_decision.confidence,
            reason=f"MiniMax 判定未通过：{llm_decision.reason}",
            needs_llm=False,
        )

    async def verify_offer_with_llm(self, text: str, parsed: ParsedCandidate | None = None) -> bool | None:
        if self.minimax is None:
            return None
        try:
            decision = await self.minimax.assess_offer(
                text=text,
                target_aliases=self.config.target_campus_aliases,
                min_time=self.config.min_start_time_obj,
                local_rule_context=self._build_offer_review_context(parsed),
            )
        except Exception as exc:
            logger.warning("MiniMax auto-claim verification failed: %r", exc)
            return None

        start_time = self._parse_time_string(decision.start_time) if decision.start_time else None
        return (
            bool(decision.is_real_offer)
            and self._is_target_campus(decision.campus)
            and start_time is not None
            and start_time >= self.config.min_start_time_obj
        )

    def _build_offer_review_context(self, parsed: ParsedCandidate | None) -> dict:
        learned_rules = load_rules(self.config.self_learning.learned_rules_path)
        return {
            "rule_reason": parsed.reason if parsed is not None else "",
            "rule_confidence": parsed.confidence if parsed is not None else None,
            "parsed_campus": parsed.campus if parsed is not None else None,
            "parsed_start_time": parsed.start_time.strftime("%H:%M") if parsed and parsed.start_time else None,
            "parsed_end_time": parsed.end_time.strftime("%H:%M") if parsed and parsed.end_time else None,
            "target_campus_key": self.config.target_campus_key,
            "target_campus_aliases": self.config.target_campus_aliases,
            "min_start_time": self.config.min_start_time,
            "include_keywords": self.config.keywords.include,
            "exclude_keywords": self.config.keywords.exclude,
            "learned_offer_rules": [
                {
                    "rule_id": rule.get("rule_id"),
                    "pattern": rule.get("pattern"),
                    "match_type": rule.get("match_type"),
                }
                for rule in learned_rules
                if isinstance(rule, dict) and rule.get("kind") == "offer"
            ][:20],
        }

    def _parse_by_rule(self, text: str) -> ParsedCandidate:
        learned_rule = match_rule(self.config.self_learning.learned_rules_path, text, {"offer"})
        if learned_rule is not None:
            campus, _ = self._detect_campus(text)
            start_time, end_time = self._detect_time_window(text)
            if campus is None and learned_rule.get("campus"):
                campus = str(learned_rule.get("campus"))
            if start_time is None and learned_rule.get("start_time"):
                start_time = self._parse_time_string(str(learned_rule.get("start_time")))
            if end_time is None and learned_rule.get("end_time"):
                end_time = self._parse_time_string(str(learned_rule.get("end_time")))
            if campus is not None and start_time is not None and start_time >= self.config.min_start_time_obj:
                confidence = learned_rule.get("confidence", 0.97)
                return ParsedCandidate(
                    is_candidate=True,
                    campus=campus,
                    start_time=start_time,
                    end_time=end_time,
                    confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.97,
                    reason=f"自学习规则命中：{learned_rule.get('rule_id', 'unknown')}",
                    needs_llm=False,
                )

        intent = self._detect_intent(text)
        campus, campus_reason = self._detect_campus(text)
        start_time, end_time = self._detect_time_window(text)
        has_offer_action = self._has_offer_action(text)
        looks_like_question = self._looks_like_question(text)

        if intent is False or looks_like_question:
            return ParsedCandidate(
                is_candidate=False,
                campus=campus,
                start_time=start_time,
                end_time=end_time,
                confidence=0.99,
                reason="命中明确排除语义",
                needs_llm=False,
            )

        if campus is None:
            return ParsedCandidate(
                is_candidate=False,
                campus=None,
                start_time=start_time,
                end_time=end_time,
                confidence=0.2,
                reason="未能解析校区，需要 LLM 兜底",
                needs_llm=True,
            )

        if not self._is_target_campus(campus):
            return ParsedCandidate(
                is_candidate=False,
                campus=campus,
                start_time=start_time,
                end_time=end_time,
                confidence=0.98,
                reason=f"校区不匹配：{campus_reason}",
                needs_llm=False,
            )

        if start_time is not None and start_time < self.config.min_start_time_obj:
            return ParsedCandidate(
                is_candidate=False,
                campus=campus,
                start_time=start_time,
                end_time=end_time,
                confidence=0.98,
                reason=f"开始时间早于阈值 {self.config.min_start_time}",
                needs_llm=False,
            )

        if start_time is not None and has_offer_action:
            confidence = 0.96 if intent is True else 0.9
            reason = (
                "规则强匹配命中：校区与时间满足，且消息包含明确送场语义"
                if intent is True
                else "规则强匹配命中：校区与时间满足，且消息包含明确动作词"
            )
            return ParsedCandidate(
                is_candidate=True,
                campus=campus,
                start_time=start_time,
                end_time=end_time,
                confidence=confidence,
                reason=reason,
                needs_llm=False,
            )

        return ParsedCandidate(
            is_candidate=False,
            campus=campus,
            start_time=start_time,
            end_time=end_time,
            confidence=0.4,
            reason="规则信息不完整，需要 LLM 兜底",
            needs_llm=True,
        )

    @staticmethod
    def _detect_intent(text: str) -> bool | None:
        if any(re.search(pattern, text) for pattern in NEGATIVE_PATTERNS):
            return False
        if any(re.search(pattern, text) for pattern in STRONG_OFFER_PATTERNS):
            return True
        return None

    @staticmethod
    def _has_offer_action(text: str) -> bool:
        return bool(OFFER_ACTION_RE.search(text))

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        if not OFFER_ACTION_RE.search(text):
            return False
        return any(marker in text for marker in QUESTION_MARKERS)

    def _detect_campus(self, text: str) -> tuple[str | None, str]:
        matched: list[tuple[str, str]] = []
        for campus_key, aliases in self.config.campus_aliases.items():
            for alias in aliases:
                if alias and alias in text:
                    matched.append((campus_key, alias))
        if not matched:
            default_campus = self.config.target_campus_aliases[0] if self.config.target_campus_aliases else None
            if default_campus is None:
                return None, "未发现校区别名"
            return default_campus, "未提校区，默认按目标校区处理"
        matched.sort(key=lambda item: len(item[1]), reverse=True)
        campus_key, alias = matched[0]
        canonical = self.config.campus_aliases[campus_key][0]
        return canonical, f"命中别名 {alias}"

    def _is_target_campus(self, campus: str | None) -> bool:
        if campus is None:
            return False
        normalized_aliases = set(self.config.target_campus_aliases)
        return campus in normalized_aliases or (
            bool(self.config.target_campus_aliases) and campus == self.config.target_campus_aliases[0]
        )

    @staticmethod
    def _detect_time_window(text: str) -> tuple[time | None, time | None]:
        if match := ARABIC_RANGE_RE.search(text):
            start_hour = int(match.group("hour"))
            start_minute = int(match.group("minute") or 0)
            end_hour = int(match.group("end"))
            end_minute = int(match.group("end_minute") or 0)
            if start_minute == 0 and end_minute == 0:
                start_hour, end_hour = _adjust_range_hours(start_hour, end_hour, text)
                return time(hour=start_hour, minute=0), time(hour=end_hour, minute=0)
            return (
                _build_time(start_hour, start_minute, text),
                _build_time(end_hour, end_minute, text),
            )

        if match := CHINESE_RANGE_RE.search(text):
            start_hour = _parse_chinese_number(match.group("hour"))
            start_minute = 30 if match.group("half") else 0
            end_hour = _parse_chinese_number(match.group("end"))
            if start_minute == 0:
                start_hour, end_hour = _adjust_range_hours(start_hour, end_hour, text)
                return time(hour=start_hour, minute=0), time(hour=end_hour, minute=0)
            return _build_time(start_hour, start_minute, text), _build_time(end_hour, 0, text)

        if match := COMPACT_RANGE_RE.search(text):
            start_hour = int(match.group("start"))
            end_hour = int(match.group("end"))
            return _build_short_range(start_hour, end_hour, text)

        if match := MIXED_COMPACT_RANGE_RE.search(text):
            start_hour = _parse_compact_hour(match.group("start"))
            end_hour = _parse_compact_hour(match.group("end"))
            return _build_short_range(start_hour, end_hour, text)

        if match := CHINESE_SIMPLE_RANGE_RE.search(text):
            start_hour = _parse_chinese_number(match.group("start"))
            end_hour = _parse_chinese_number(match.group("end"))
            return _build_short_range(start_hour, end_hour, text)

        if match := CHINESE_COMPACT_RANGE_RE.search(text):
            start_hour = _parse_chinese_number(match.group("start"))
            end_hour = _parse_chinese_number(match.group("end"))
            return _build_short_range(start_hour, end_hour, text)

        if match := ARABIC_POINT_RE.search(text):
            hour = int(match.group("hour"))
            minute = int(match.group("minute") or 0)
            if match.group("half"):
                minute = 30
            return _build_time(hour, minute, text), None

        if match := CHINESE_POINT_RE.search(text):
            hour = _parse_chinese_number(match.group("hour"))
            minute = 30 if match.group("half") else 0
            return _build_time(hour, minute, text), None

        return None, None

    @staticmethod
    def _parse_time_string(value: str) -> time | None:
        if not value:
            return None
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        return time(hour=hour, minute=minute)
