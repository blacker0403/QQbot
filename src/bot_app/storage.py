from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import json
import logging

from bot_app.models import ApprovalTask, ApprovalTaskKind, ApprovalTaskStatus, AutoRecallTask, ClaimMode, PersistedState, ResolvedSlot, SwapWatchRule

logger = logging.getLogger(__name__)

HAVE_SLOT_KEEP_AHEAD = timedelta(hours=8)
WANT_SLOT_KEEP_AHEAD = timedelta(hours=5)


class JsonStateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._state = self._read_from_disk()

    def _read_from_disk(self) -> PersistedState:
        if not self.path.exists():
            return PersistedState()
        with self.path.open("r", encoding="utf-8") as file:
            return PersistedState.model_validate(json.load(file))

    def _write_to_disk(self) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(self._state.model_dump(mode="json"), file, ensure_ascii=False, indent=2)
        tmp_path.replace(self.path)

    async def initialize_secondary_owner_qq(self, qq: str | None) -> None:
        if not qq:
            return
        async with self._lock:
            if self._state.secondary_owner_qq:
                return
            self._state.secondary_owner_qq = qq
            self._write_to_disk()

    async def create_task(self, task: ApprovalTask) -> None:
        async with self._lock:
            self._state.tasks[task.task_id] = task
            self._write_to_disk()

    async def update_task(self, task: ApprovalTask) -> None:
        async with self._lock:
            self._state.tasks[task.task_id] = task
            self._write_to_disk()

    async def get_task_by_token(self, token: str) -> ApprovalTask | None:
        async with self._lock:
            for task in self._state.tasks.values():
                if task.token == token:
                    return task
        return None

    async def claim_pending_task_by_token(self, token: str) -> tuple[ApprovalTask | None, bool]:
        async with self._lock:
            for task_id, task in self._state.tasks.items():
                if task.token != token:
                    continue
                if task.status != ApprovalTaskStatus.PENDING:
                    return task, False
                updated = task.model_copy(update={"status": ApprovalTaskStatus.PROCESSING})
                self._state.tasks[task_id] = updated
                self._write_to_disk()
                return updated, True
        return None, False

    async def find_task(
        self,
        task_kind: ApprovalTaskKind,
        message_id: str,
        matched_rule_id: str | None = None,
    ) -> ApprovalTask | None:
        async with self._lock:
            for task in self._state.tasks.values():
                if task.task_kind != task_kind:
                    continue
                if task.message_id != message_id:
                    continue
                if matched_rule_id is not None and task.matched_rule_id != matched_rule_id:
                    continue
                return task
        return None

    async def list_pending_tasks(self, now: datetime | None = None) -> list[ApprovalTask]:
        now = now or datetime.now()
        async with self._lock:
            tasks = list(self._state.tasks.values())
        result: list[ApprovalTask] = []
        for task in tasks:
            if task.status == ApprovalTaskStatus.PENDING and task.expires_at > now:
                result.append(task)
        return sorted(result, key=lambda item: item.created_at)

    async def expire_overdue_tasks(self, now: datetime | None = None) -> list[ApprovalTask]:
        now = now or datetime.now()
        expired: list[ApprovalTask] = []
        async with self._lock:
            changed = False
            for task_id, task in self._state.tasks.items():
                if task.status == ApprovalTaskStatus.PENDING and task.expires_at <= now:
                    self._state.tasks[task_id] = task.model_copy(
                        update={"status": ApprovalTaskStatus.EXPIRED}
                    )
                    expired.append(self._state.tasks[task_id])
                    changed = True
            if changed:
                self._write_to_disk()
        if expired:
            logger.info("Expired %s pending approval task(s).", len(expired))
        return expired

    async def set_last_claimed_at(self, claimed_at: datetime) -> None:
        async with self._lock:
            self._state.last_claimed_at = claimed_at
            self._write_to_disk()

    async def get_last_claimed_at(self) -> datetime | None:
        async with self._lock:
            return self._state.last_claimed_at

    async def clear_last_claimed_at(self) -> None:
        async with self._lock:
            self._state.last_claimed_at = None
            self._write_to_disk()

    async def get_claim_mode(self) -> ClaimMode:
        async with self._lock:
            return self._state.claim_mode

    async def set_claim_mode(self, mode: ClaimMode) -> None:
        async with self._lock:
            self._state.claim_mode = mode
            self._write_to_disk()

    async def is_claim_listening_paused(self) -> bool:
        async with self._lock:
            return self._state.claim_listening_paused

    async def set_claim_listening_paused(self, paused: bool) -> None:
        async with self._lock:
            self._state.claim_listening_paused = paused
            self._write_to_disk()

    async def get_pending_auto_recall(self) -> AutoRecallTask | None:
        async with self._lock:
            return self._state.pending_auto_recall

    async def set_pending_auto_recall(self, recall_task: AutoRecallTask | None) -> None:
        async with self._lock:
            self._state.pending_auto_recall = recall_task
            self._write_to_disk()

    async def get_secondary_owner_qq(self) -> str | None:
        async with self._lock:
            return self._state.secondary_owner_qq

    async def set_secondary_owner_qq(self, qq: str | None) -> None:
        async with self._lock:
            self._state.secondary_owner_qq = qq
            self._write_to_disk()

    async def save_swap_watch_rule(self, rule: SwapWatchRule, now: datetime | None = None) -> SwapWatchRule | None:
        now = now or datetime.now()
        normalized_rule = self._prune_swap_watch_rule(rule, now)
        async with self._lock:
            if normalized_rule is None:
                self._state.swap_watch_rules.pop(rule.rule_id, None)
                self._write_to_disk()
                return None
            normalized_signature = self._rule_signature(normalized_rule)
            duplicate_rule: SwapWatchRule | None = None
            changed = False
            for existing_rule_id, existing_rule in list(self._state.swap_watch_rules.items()):
                pruned_existing = self._prune_swap_watch_rule(existing_rule, now)
                if pruned_existing is None:
                    self._state.swap_watch_rules.pop(existing_rule_id, None)
                    changed = True
                    continue
                if pruned_existing != existing_rule:
                    self._state.swap_watch_rules[existing_rule_id] = pruned_existing
                    existing_rule = pruned_existing
                    changed = True
                if existing_rule_id == normalized_rule.rule_id:
                    continue
                if self._rule_signature(existing_rule) != normalized_signature:
                    continue
                if duplicate_rule is None:
                    duplicate_rule = existing_rule
                    continue
                self._state.swap_watch_rules.pop(existing_rule_id, None)
                changed = True
            if duplicate_rule is not None:
                if changed:
                    self._write_to_disk()
                return duplicate_rule
            self._state.swap_watch_rules[normalized_rule.rule_id] = normalized_rule
            self._write_to_disk()
            return normalized_rule

    async def get_swap_watch_rule(self, rule_id: str, now: datetime | None = None) -> SwapWatchRule | None:
        now = now or datetime.now()
        async with self._lock:
            rule = self._state.swap_watch_rules.get(rule_id)
            if rule is None:
                return None
            normalized_rule = self._prune_swap_watch_rule(rule, now)
            if normalized_rule is None:
                self._state.swap_watch_rules.pop(rule_id, None)
                self._write_to_disk()
                return None
            if normalized_rule != rule:
                self._state.swap_watch_rules[rule_id] = normalized_rule
                self._write_to_disk()
            return normalized_rule

    async def list_swap_watch_rules(self, now: datetime | None = None) -> list[SwapWatchRule]:
        now = now or datetime.now()
        async with self._lock:
            rules: list[SwapWatchRule] = []
            changed = False
            seen_signatures: set[tuple] = set()
            for rule_id, rule in list(self._state.swap_watch_rules.items()):
                normalized_rule = self._prune_swap_watch_rule(rule, now)
                if normalized_rule is None:
                    self._state.swap_watch_rules.pop(rule_id, None)
                    changed = True
                    continue
                if normalized_rule != rule:
                    self._state.swap_watch_rules[rule_id] = normalized_rule
                    changed = True
                signature = self._rule_signature(normalized_rule)
                if signature in seen_signatures:
                    self._state.swap_watch_rules.pop(rule_id, None)
                    changed = True
                    continue
                seen_signatures.add(signature)
                rules.append(normalized_rule)
            if changed:
                self._write_to_disk()
        return sorted(rules, key=lambda item: item.created_at)

    async def remove_swap_watch_rule(self, rule_id: str) -> SwapWatchRule | None:
        async with self._lock:
            removed = self._state.swap_watch_rules.pop(rule_id, None)
            if removed is not None:
                self._write_to_disk()
            return removed

    async def clear_swap_watch_rules(self) -> int:
        async with self._lock:
            count = len(self._state.swap_watch_rules)
            if count:
                self._state.swap_watch_rules.clear()
                self._write_to_disk()
            return count

    async def cancel_pending_tasks(self, now: datetime | None = None) -> list[ApprovalTask]:
        now = now or datetime.now()
        cancelled: list[ApprovalTask] = []
        async with self._lock:
            changed = False
            for task_id, task in self._state.tasks.items():
                if task.status == ApprovalTaskStatus.PENDING and task.expires_at > now:
                    updated = task.model_copy(update={"status": ApprovalTaskStatus.CANCELLED})
                    self._state.tasks[task_id] = updated
                    cancelled.append(updated)
                    changed = True
            if changed:
                self._write_to_disk()
        if cancelled:
            logger.info("Cancelled %s pending approval task(s).", len(cancelled))
        return cancelled

    @staticmethod
    def _slot_start_datetime(slot: ResolvedSlot) -> datetime | None:
        try:
            return datetime.fromisoformat(f"{slot.date}T{slot.start_time}")
        except ValueError:
            return None

    def _should_keep_swap_slot(self, slot: ResolvedSlot, now: datetime, min_lead: timedelta) -> bool:
        slot_start = self._slot_start_datetime(slot)
        if slot_start is None:
            return False
        return slot_start - now >= min_lead

    def _prune_swap_watch_rule(self, rule: SwapWatchRule, now: datetime) -> SwapWatchRule | None:
        have_slots = [
            slot
            for slot in rule.have_slots
            if self._should_keep_swap_slot(slot, now, HAVE_SLOT_KEEP_AHEAD)
        ]
        want_slots = [
            slot
            for slot in rule.want_slots
            if self._should_keep_swap_slot(slot, now, WANT_SLOT_KEEP_AHEAD)
        ]
        if not have_slots or not want_slots:
            return None
        if have_slots == rule.have_slots and want_slots == rule.want_slots:
            return rule
        return rule.model_copy(update={"have_slots": have_slots, "want_slots": want_slots})

    @staticmethod
    def _slot_signature(slot: ResolvedSlot) -> tuple:
        return (
            slot.date,
            slot.start_time,
            slot.end_time,
            slot.campus,
            slot.match_mode,
        )

    def _rule_signature(self, rule: SwapWatchRule) -> tuple:
        return (
            tuple(self._slot_signature(slot) for slot in rule.have_slots),
            tuple(self._slot_signature(slot) for slot in rule.want_slots),
        )
