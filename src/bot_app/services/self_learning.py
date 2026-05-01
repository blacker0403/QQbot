from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import re
import sqlite3
import uuid
from typing import Any, Iterable, Sequence

from nonebot.adapters.onebot.v11 import Bot

from bot_app.config import AppConfig
from bot_app.models import ClaimMode, IncomingGroupMessage
from bot_app.services.learned_rules import load_rules, save_rules
from bot_app.services.workflow import ClaimWorkflow

HISTORY_FILE_SUFFIXES = {".txt", ".log", ".json", ".jsonl", ".db", ".sqlite", ".sqlite3"}
TEXT_FILE_SUFFIXES = {".txt", ".log", ".json", ".jsonl"}
SQLITE_FILE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
HISTORY_SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "emoji",
    "image",
    "pic",
    "ptt",
    "thumb",
    "video",
    "cache",
}
HISTORY_HINTS = ("send", "song", "offer", "swap", "change", "field", "court", "venue", "history", "message", "msg", "group")
MESSAGE_KEYWORDS = ("送", "出", "转", "场", "场地", "一片", "换", "想换", "要换", "求", "收")
AUTO_APPLY_CONFIDENCE = 0.95


@dataclass(slots=True)
class SelfLearningSample:
    kind: str
    text: str
    source_group_id: str


@dataclass(slots=True)
class SelfLearningPreview:
    samples: list[SelfLearningSample]


@dataclass(slots=True)
class SelfLearningRunResult:
    text: str
    group_id: str
    expected_action: str
    actual_action: str
    consistent: bool


@dataclass(slots=True)
class SelfLearningRunReport:
    results: list[SelfLearningRunResult]


class SelfLearningService:
    def __init__(self, config: AppConfig, workflow: ClaimWorkflow) -> None:
        self.config = config
        self.workflow = workflow

    async def observe_group_message(
        self,
        incoming: IncomingGroupMessage,
        bot_self_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self._can_learn_from_message(incoming, bot_self_id):
            return None

        text = incoming.raw_text.strip()
        if self.workflow.exchange_parser.looks_like_exchange(text):
            parsed_exchange = await self.workflow.exchange_parser.parse(text, incoming.timestamp)
            if (
                parsed_exchange.is_exchange_candidate
                and parsed_exchange.confidence >= AUTO_APPLY_CONFIDENCE
                and parsed_exchange.their_have_slots
                and parsed_exchange.their_want_slots
            ):
                return self._save_learned_rule(
                    {
                        "kind": "exchange",
                        "pattern": text,
                        "match_type": "exact",
                        "source_group_id": incoming.group_id,
                        "confidence": parsed_exchange.confidence,
                        "their_have_slots": [
                            slot.model_dump(mode="json") for slot in parsed_exchange.their_have_slots
                        ],
                        "their_want_slots": [
                            slot.model_dump(mode="json") for slot in parsed_exchange.their_want_slots
                        ],
                        "reason": parsed_exchange.reason,
                    }
                )

        if not self.workflow.prefilter.match(text):
            return None

        parsed_offer = await self.workflow.parser.parse(text)
        if (
            parsed_offer.is_candidate
            and parsed_offer.confidence >= AUTO_APPLY_CONFIDENCE
            and parsed_offer.campus is not None
            and parsed_offer.start_time is not None
        ):
            return self._save_learned_rule(
                {
                    "kind": "offer",
                    "pattern": text,
                    "match_type": "exact",
                    "source_group_id": incoming.group_id,
                    "confidence": parsed_offer.confidence,
                    "campus": parsed_offer.campus,
                    "start_time": parsed_offer.start_time.strftime("%H:%M"),
                    "end_time": parsed_offer.end_time.strftime("%H:%M") if parsed_offer.end_time else None,
                    "reason": parsed_offer.reason,
                }
            )
        return None

    def load_history_records(self) -> list[dict[str, str]]:
        source_group_id = self.config.self_learning.source_group_id
        records: list[dict[str, str]] = []
        history_path = Path("data") / f"group_{self.config.self_learning.source_group_id}_history.txt"
        if history_path.exists():
            for line in history_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                text = line.strip()
                if text:
                    records.append({"group_id": source_group_id, "text": text})
        for task in self.workflow.store._state.tasks.values():
            if task.group_id != source_group_id or not task.raw_text:
                continue
            records.append({"group_id": source_group_id, "text": task.raw_text})
        records.extend(self.load_discovered_history_records())
        return self._dedupe_records(records)

    def load_discovered_history_records(self) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for path in self.discover_history_files():
            if path.suffix.lower() in SQLITE_FILE_SUFFIXES:
                records.extend(self._records_from_sqlite(path))
            else:
                records.extend(self._records_from_text_file(path))
        return self._dedupe_records(records)

    def discover_history_files(self) -> list[Path]:
        found: list[Path] = []
        seen: set[Path] = set()
        max_files = self.config.self_learning.history_max_files
        for root in self._history_search_roots():
            if not root.exists() or not root.is_dir():
                continue
            for path in self._iter_history_files(root):
                try:
                    resolved = path.resolve()
                    size = resolved.stat().st_size
                except OSError:
                    continue
                if resolved in seen or size <= 0 or size > self.config.self_learning.history_max_file_bytes:
                    continue
                seen.add(resolved)
                found.append(resolved)
                if len(found) >= max_files:
                    return found
        return found

    def preview_from_records(self, records: Sequence[dict[str, Any]], limit: int = 20) -> SelfLearningPreview:
        samples: list[SelfLearningSample] = []
        source_group_id = self.config.self_learning.source_group_id
        for record in records:
            group_id = str(record.get("group_id", ""))
            text = str(record.get("text", "")).strip()
            if group_id != source_group_id or not text or text.startswith("/"):
                continue
            kind = self.classify_text(text)
            if kind is None:
                continue
            samples.append(SelfLearningSample(kind=kind, text=text, source_group_id=group_id))
            if len(samples) >= limit:
                break
        return SelfLearningPreview(samples=samples)

    def preview(self, limit: int = 20) -> SelfLearningPreview:
        return self.preview_from_records(self.load_history_records(), limit=limit)

    def classify_text(self, text: str) -> str | None:
        if self.workflow.exchange_parser.looks_like_exchange(text):
            return "exchange"
        if not self.workflow.prefilter.match(text):
            return None
        parsed = self.workflow.parser._parse_by_rule(text)
        if parsed.is_candidate or parsed.needs_llm:
            return "offer"
        return None

    async def run_offline(
        self,
        bot: Bot,
        texts: Sequence[str],
        reference_time: datetime | None = None,
    ) -> SelfLearningRunReport:
        reference_time = reference_time or datetime.now()
        test_group_id = self.config.self_learning.test_group_id
        if test_group_id == self.config.self_learning.source_group_id:
            raise ValueError("自学习测试群不能等于学习来源群，禁止向学习群发送测试消息")

        original_state = self.workflow.store._state.model_copy(deep=True)
        results: list[SelfLearningRunResult] = []
        try:
            await self.workflow.store.set_claim_mode(ClaimMode.MANUAL)
            for index, text in enumerate(texts, start=1):
                expected_action = self.classify_text(text) or "none"
                private_count = len(getattr(bot, "private_messages", []))
                group_count = len(getattr(bot, "group_messages", []))
                incoming = IncomingGroupMessage(
                    group_id=test_group_id,
                    user_id=self.config.owner_qq,
                    message_id=f"selflearn-{index}",
                    nickname="自学习测试",
                    raw_text=text,
                    timestamp=reference_time,
                )
                await self.workflow.handle_group_message(bot, incoming)
                actual_action = self._detect_action(bot, private_count, group_count)
                response_summary = self._response_summary(bot, private_count, group_count)
                consistent = await self._assess_consistency(
                    text=text,
                    expected_action=expected_action,
                    actual_action=actual_action,
                    response_summary=response_summary,
                )
                results.append(
                    SelfLearningRunResult(
                        text=text,
                        group_id=test_group_id,
                        expected_action=expected_action,
                        actual_action=actual_action,
                        consistent=consistent,
                    )
                )
        finally:
            self.workflow.store._state = original_state
            self.workflow.store._write_to_disk()
        return SelfLearningRunReport(results=results)

    async def run_live(self, bot: Bot, texts: Sequence[str]) -> SelfLearningRunReport:
        test_group_id = self.config.self_learning.test_group_id
        if test_group_id == self.config.self_learning.source_group_id:
            raise ValueError("自学习测试群不能等于学习来源群，禁止向学习群发送测试消息")
        original_mode = await self.workflow.store.get_claim_mode()
        results: list[SelfLearningRunResult] = []
        try:
            await self.workflow.store.set_claim_mode(ClaimMode.MANUAL)
            for text in texts:
                await bot.send_group_msg(
                    group_id=int(test_group_id) if test_group_id.isdigit() else test_group_id,
                    message=text,
                )
                expected_action = self.classify_text(text) or "none"
                consistent = await self._assess_consistency(
                    text=text,
                    expected_action=expected_action,
                    actual_action="sent",
                    response_summary="已向测试群发送样本，等待 bot 自消息上报链路处理",
                )
                results.append(
                    SelfLearningRunResult(
                        text=text,
                        group_id=test_group_id,
                        expected_action=expected_action,
                        actual_action="sent",
                        consistent=consistent,
                    )
                )
        finally:
            await self.workflow.store.set_claim_mode(original_mode)
        self._save_report(results)
        return SelfLearningRunReport(results=results)

    def save_candidate_rule(
        self,
        *,
        kind: str,
        pattern: str,
        match_type: str = "exact",
        **extra: Any,
    ) -> str:
        candidate_id = uuid.uuid4().hex[:8].upper()
        path = Path(self.config.self_learning.candidates_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read_json(path)
        candidates = data.setdefault("candidates", [])
        candidates.append(
            {
                "candidate_id": candidate_id,
                "kind": kind,
                "pattern": pattern,
                "match_type": match_type,
                **extra,
            }
        )
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return candidate_id

    def apply_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        candidates_path = Path(self.config.self_learning.candidates_path)
        data = self._read_json(candidates_path)
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list):
            return None
        selected = None
        remaining = []
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("candidate_id") == candidate_id:
                selected = candidate
            else:
                remaining.append(candidate)
        if selected is None:
            return None

        rule = dict(selected)
        rule["rule_id"] = rule.pop("candidate_id")
        rules = load_rules(self.config.self_learning.learned_rules_path)
        rules.append(rule)
        save_rules(self.config.self_learning.learned_rules_path, rules)
        data["candidates"] = remaining
        candidates_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return rule

    def _save_learned_rule(self, rule: dict[str, Any]) -> dict[str, Any] | None:
        rules = load_rules(self.config.self_learning.learned_rules_path)
        for existing in rules:
            if (
                existing.get("kind") == rule.get("kind")
                and existing.get("pattern") == rule.get("pattern")
                and existing.get("match_type", "exact") == rule.get("match_type", "exact")
            ):
                return None
        saved_rule = {"rule_id": uuid.uuid4().hex[:8].upper(), **rule}
        rules.append(saved_rule)
        save_rules(self.config.self_learning.learned_rules_path, rules)
        return saved_rule

    def _can_learn_from_message(self, incoming: IncomingGroupMessage, bot_self_id: str | None) -> bool:
        text = incoming.raw_text.strip()
        if not text or text.startswith("/") or text in {"1", "0"}:
            return False
        if bot_self_id and incoming.user_id == bot_self_id:
            return False
        if incoming.group_id == self.config.self_learning.test_group_id:
            return False
        learnable_groups = set(self.config.target_groups)
        learnable_groups.add(self.config.self_learning.source_group_id)
        return incoming.group_id in learnable_groups

    def _save_report(self, results: list[SelfLearningRunResult]) -> None:
        path = Path(self.config.self_learning.candidates_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read_json(path)
        data["last_report"] = [asdict(result) for result in results]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _assess_consistency(
        self,
        *,
        text: str,
        expected_action: str,
        actual_action: str,
        response_summary: str,
    ) -> bool:
        provider = self.workflow.parser.minimax or self.workflow.exchange_parser.minimax
        assess = getattr(provider, "assess_selflearn_consistency", None) if provider is not None else None
        if assess is not None:
            try:
                return bool(
                    await assess(
                        text=text,
                        expected_action=expected_action,
                        actual_action=actual_action,
                        response_summary=response_summary,
                    )
                )
            except Exception:
                return self._is_consistent(expected_action, actual_action)
        return self._is_consistent(expected_action, actual_action)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _detect_action(bot: Bot, private_count: int, group_count: int) -> str:
        new_group_messages = getattr(bot, "group_messages", [])[group_count:]
        if new_group_messages:
            return "group_reply"
        new_private_messages = getattr(bot, "private_messages", [])[private_count:]
        for item in new_private_messages:
            message = str(item.get("message", ""))
            if "换场匹配命中" in message:
                return "swap_match"
            if "候选场地" in message or "命中但在冷却中" in message:
                return "claim"
        return "none"

    @staticmethod
    def _response_summary(bot: Bot, private_count: int, group_count: int) -> str:
        new_group_messages = getattr(bot, "group_messages", [])[group_count:]
        new_private_messages = getattr(bot, "private_messages", [])[private_count:]
        parts = [str(item.get("message", "")) for item in new_private_messages]
        parts.extend(str(item.get("message", "")) for item in new_group_messages)
        summary = "\n".join(part for part in parts if part)
        return summary[:1000]

    @staticmethod
    def _is_consistent(expected_action: str, actual_action: str) -> bool:
        if expected_action == "offer":
            return actual_action == "claim"
        if expected_action == "exchange":
            return actual_action == "swap_match"
        return actual_action == "none"

    def _history_search_roots(self) -> list[Path]:
        roots: list[Path] = []
        for root_text in self.config.self_learning.history_search_roots:
            root_text = os.path.expandvars(os.path.expanduser(root_text))
            if root_text:
                roots.append(Path(root_text))

        if not self.config.self_learning.history_auto_search_enabled:
            return self._dedupe_paths(roots)

        roots.extend([Path("data"), Path("logs")])
        user_profile = os.getenv("USERPROFILE")
        appdata = os.getenv("APPDATA")
        if user_profile:
            roots.append(Path(user_profile) / "Documents" / "Tencent Files")
        if appdata:
            roots.extend([Path(appdata) / "QQ", Path(appdata) / "Tencent"])
        return self._dedupe_paths(roots)

    def _iter_history_files(self, root: Path) -> Iterable[Path]:
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if child.name.lower() not in HISTORY_SKIP_DIR_NAMES:
                        stack.append(child)
                    continue
                if child.suffix.lower() not in HISTORY_FILE_SUFFIXES:
                    continue
                if self._looks_like_history_path(child):
                    yield child

    def _looks_like_history_path(self, path: Path) -> bool:
        source_group_id = self.config.self_learning.source_group_id
        text = str(path).lower()
        return source_group_id in text or any(hint in text for hint in HISTORY_HINTS)

    def _records_from_text_file(self, path: Path) -> list[dict[str, str]]:
        try:
            payload = path.read_bytes()[: self.config.self_learning.history_max_file_bytes]
        except OSError:
            return []
        text = payload.decode("utf-8", errors="ignore")
        records: list[dict[str, str]] = []
        path_has_group = self.config.self_learning.source_group_id in str(path)
        for line in text.splitlines():
            records.extend(self._records_from_text_line(line, path_has_group=path_has_group))
        return records

    def _records_from_text_line(self, line: str, *, path_has_group: bool) -> list[dict[str, str]]:
        source_group_id = self.config.self_learning.source_group_id
        compact = line.strip()
        if not compact:
            return []
        has_group = source_group_id in compact or path_has_group
        if not has_group:
            return []

        texts = self._extract_message_texts(compact)
        if not texts:
            texts = [compact]
        return [
            {"group_id": source_group_id, "text": text}
            for text in texts
            if self._is_relevant_history_text(text)
        ]

    def _extract_message_texts(self, line: str) -> list[str]:
        texts: list[str] = []
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            texts.extend(self._walk_json_texts(parsed))

        field_re = re.compile(r'"(?:raw_text|text|message|content|msg)"\s*:\s*"((?:\\.|[^"\\])*)"')
        for match in field_re.finditer(line):
            raw = match.group(1)
            try:
                texts.append(json.loads(f'"{raw}"'))
            except json.JSONDecodeError:
                texts.append(raw)
        return self._dedupe_strings(texts)

    def _walk_json_texts(self, value: Any) -> list[str]:
        if isinstance(value, dict):
            texts: list[str] = []
            for key, item in value.items():
                key_text = str(key).lower()
                if key_text in {"raw_text", "text", "message", "content", "msg"} and isinstance(item, str):
                    texts.append(item)
                else:
                    texts.extend(self._walk_json_texts(item))
            return texts
        if isinstance(value, list):
            texts: list[str] = []
            for item in value:
                texts.extend(self._walk_json_texts(item))
            return texts
        return []

    def _records_from_sqlite(self, path: Path) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            return records
        try:
            tables = conn.execute("select name from sqlite_master where type = 'table'").fetchall()
            for (table_name,) in tables:
                records.extend(self._records_from_sqlite_table(conn, table_name))
        except sqlite3.Error:
            return records
        finally:
            conn.close()
        return records

    def _records_from_sqlite_table(self, conn: sqlite3.Connection, table_name: str) -> list[dict[str, str]]:
        try:
            columns = conn.execute(f"pragma table_info({self._quote_sql_identifier(table_name)})").fetchall()
        except sqlite3.Error:
            return []
        column_names = [str(column[1]) for column in columns]
        if not column_names:
            return []
        text_columns = [
            name
            for name in column_names
            if any(hint in name.lower() for hint in ("group", "room", "peer", "text", "msg", "message", "content", "raw"))
        ]
        if not text_columns:
            return []
        quoted = ", ".join(self._quote_sql_identifier(name) for name in text_columns)
        try:
            rows = conn.execute(f"select {quoted} from {self._quote_sql_identifier(table_name)} limit 5000").fetchall()
        except sqlite3.Error:
            return []

        records: list[dict[str, str]] = []
        source_group_id = self.config.self_learning.source_group_id
        for row in rows:
            cells = ["" if value is None else str(value) for value in row]
            joined = "\n".join(cells)
            if source_group_id not in joined:
                continue
            row_records: list[dict[str, str]] = []
            for cell in cells:
                if self._is_relevant_history_text(cell) and source_group_id not in cell:
                    row_records.append({"group_id": source_group_id, "text": cell.strip()})
            if not row_records:
                row_records.extend(self._records_from_text_line(joined, path_has_group=False))
            records.extend(row_records)
        return records

    @staticmethod
    def _quote_sql_identifier(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    @staticmethod
    def _is_relevant_history_text(text: str) -> bool:
        compact = text.strip()
        return bool(compact) and not compact.startswith("/") and any(keyword in compact for keyword in MESSAGE_KEYWORDS)

    @staticmethod
    def _dedupe_records(records: Sequence[dict[str, str]]) -> list[dict[str, str]]:
        seen: set[tuple[str, str]] = set()
        deduped: list[dict[str, str]] = []
        for record in records:
            group_id = str(record.get("group_id", ""))
            text = str(record.get("text", "")).strip()
            key = (group_id, text)
            if not group_id or not text or key in seen:
                continue
            seen.add(key)
            deduped.append({"group_id": group_id, "text": text})
        return deduped

    @staticmethod
    def _dedupe_paths(paths: Sequence[Path]) -> list[Path]:
        seen: set[Path] = set()
        deduped: list[Path] = []
        for path in paths:
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    @staticmethod
    def _dedupe_strings(values: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            text = value.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            deduped.append(text)
        return deduped
