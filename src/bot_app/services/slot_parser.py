from __future__ import annotations

from datetime import date, datetime, timedelta
import re

from bot_app.config import AppConfig
from bot_app.models import ResolvedSlot
from bot_app.services.semantic_parse import SemanticParseService, _build_time, _parse_chinese_number

SLOT_SPLIT_RE = re.compile(r"\s*(?:/|／|,|，|、|或|or|OR)\s*")
DATE_PREFIX_RE = re.compile(r"^(?:我有|有|我想换|想换|换|求|收|想要|需要)\s*")
RELATIVE_DATE_RE = re.compile(r"(今天|今晚|明天|明晚|后天)")
WEEKDAY_RE = re.compile(r"(?:周|星期)([一二三四五六日天])")
FULL_DATE_RE = re.compile(r"(?P<year>\d{4})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})日?")
SHORT_DATE_RE = re.compile(r"(?<!\d)(?P<month>\d{1,2})(?P<sep>[-/.月])(?P<day>\d{1,2})日?(?!\d)")
DAY_ONLY_DATE_RE = re.compile(r"(?<!\d)(?P<day>\d{1,2})号(?!\d)")
AFTER_TIME_RE = re.compile(
    r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]+)\s*点?(?:半)?\s*(?:以后|之后|后)"
)

WEEKDAY_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}

RELATIVE_DAY_OFFSETS = {
    "今天": 0,
    "今晚": 0,
    "明天": 1,
    "明晚": 1,
    "后天": 2,
}


class SlotParser:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def parse_slot_list(
        self,
        text: str,
        reference_time: datetime,
        fallback_date: str | None = None,
        fallback_campus: str | None = None,
    ) -> list[ResolvedSlot]:
        normalized = text.strip()
        if not normalized:
            return []

        tokens = [part.strip() for part in SLOT_SPLIT_RE.split(normalized) if part.strip()]
        result: list[ResolvedSlot] = []
        inherited_date = fallback_date
        inherited_campus = fallback_campus
        for token in tokens:
            slot = self.parse_slot(
                token,
                reference_time=reference_time,
                fallback_date=inherited_date,
                fallback_campus=inherited_campus,
            )
            if slot is None:
                continue
            result.append(slot)
            inherited_date = slot.date
            inherited_campus = slot.campus
        return result

    def parse_swap_rule_list(
        self,
        text: str,
        reference_time: datetime,
        fallback_date: str | None = None,
        fallback_campus: str | None = None,
    ) -> list[ResolvedSlot]:
        normalized = text.strip()
        if not normalized:
            return []

        tokens = [part.strip() for part in SLOT_SPLIT_RE.split(normalized) if part.strip()]
        result: list[ResolvedSlot] = []
        inherited_date = fallback_date
        inherited_campus = fallback_campus
        for token in tokens:
            slot = self.parse_swap_rule(
                token,
                reference_time=reference_time,
                fallback_date=inherited_date,
                fallback_campus=inherited_campus,
            )
            if slot is None:
                continue
            result.append(slot)
            inherited_date = slot.date
            inherited_campus = slot.campus
        return result

    def parse_slot(
        self,
        text: str,
        reference_time: datetime,
        fallback_date: str | None = None,
        fallback_campus: str | None = None,
    ) -> ResolvedSlot | None:
        normalized = DATE_PREFIX_RE.sub("", text.strip())
        normalized = normalized.replace("的", "")
        if not normalized:
            return None

        start_time, end_time = SemanticParseService._detect_time_window(normalized)
        if start_time is None or end_time is None:
            return None

        resolved_date = self._resolve_date(normalized, reference_time, start_time, fallback_date)
        if resolved_date is None:
            return None

        campus = self._detect_campus(normalized, fallback_campus)
        return ResolvedSlot(
            date=resolved_date,
            start_time=start_time.strftime("%H:%M"),
            end_time=end_time.strftime("%H:%M"),
            campus=campus,
            raw_text=text.strip(),
        )

    def parse_swap_rule(
        self,
        text: str,
        reference_time: datetime,
        fallback_date: str | None = None,
        fallback_campus: str | None = None,
    ) -> ResolvedSlot | None:
        exact_slot = self.parse_slot(
            text,
            reference_time=reference_time,
            fallback_date=fallback_date,
            fallback_campus=fallback_campus,
        )
        if exact_slot is not None:
            return exact_slot

        normalized = DATE_PREFIX_RE.sub("", text.strip())
        normalized = normalized.replace("的", "")
        if not normalized:
            return None

        start_time = self._detect_after_time(normalized)
        if start_time is None:
            return None

        resolved_date = self._resolve_date(normalized, reference_time, start_time, fallback_date)
        if resolved_date is None:
            return None

        campus = self._detect_campus(normalized, fallback_campus)
        return ResolvedSlot(
            date=resolved_date,
            start_time=start_time.strftime("%H:%M"),
            end_time=None,
            campus=campus,
            match_mode="start_after",
            raw_text=text.strip(),
        )

    def _detect_campus(self, text: str, fallback_campus: str | None) -> str:
        matched: list[tuple[str, str]] = []
        for campus_key, aliases in self.config.campus_aliases.items():
            for alias in aliases:
                if alias and alias in text:
                    matched.append((campus_key, alias))
        if matched:
            matched.sort(key=lambda item: len(item[1]), reverse=True)
            campus_key, _ = matched[0]
            return self.config.campus_aliases[campus_key][0]
        if fallback_campus:
            return fallback_campus
        if self.config.target_campus_aliases:
            return self.config.target_campus_aliases[0]
        return self.config.target_campus_key

    def _resolve_date(
        self,
        text: str,
        reference_time: datetime,
        start_time,
        fallback_date: str | None,
    ) -> str | None:
        if match := FULL_DATE_RE.search(text):
            resolved = date(
                year=int(match.group("year")),
                month=int(match.group("month")),
                day=int(match.group("day")),
            )
            return resolved.isoformat()

        if match := RELATIVE_DATE_RE.search(text):
            offset = RELATIVE_DAY_OFFSETS[match.group(1)]
            return (reference_time.date() + timedelta(days=offset)).isoformat()

        if match := WEEKDAY_RE.search(text):
            target_weekday = WEEKDAY_MAP[match.group(1)]
            current_date = reference_time.date()
            days_ahead = (target_weekday - current_date.weekday()) % 7
            resolved = current_date + timedelta(days=days_ahead)
            if days_ahead == 0 and start_time <= reference_time.time():
                resolved = resolved + timedelta(days=7)
            elif days_ahead < 0:
                resolved = resolved + timedelta(days=7)
            return resolved.isoformat()

        if match := SHORT_DATE_RE.search(text):
            month = int(match.group("month"))
            day = int(match.group("day"))
            if day > 12 or match.group("sep") != "-":
                year = reference_time.year
                try:
                    resolved = date(year=year, month=month, day=day)
                except ValueError:
                    return fallback_date
                if resolved < reference_time.date():
                    try:
                        resolved = date(year=year + 1, month=month, day=day)
                    except ValueError:
                        return fallback_date
                return resolved.isoformat()

        if match := DAY_ONLY_DATE_RE.search(text):
            day = int(match.group("day"))
            current_date = reference_time.date()
            for offset in range(4):
                resolved = current_date + timedelta(days=offset)
                if resolved.day == day:
                    return resolved.isoformat()
            return fallback_date

        return fallback_date

    @staticmethod
    def _detect_after_time(text: str):
        match = AFTER_TIME_RE.search(text)
        if match is None:
            return None
        hour_text = match.group("hour")
        hour = int(hour_text) if hour_text.isdigit() else _parse_chinese_number(hour_text)
        return _build_time(hour, 0, text)
