from __future__ import annotations

from datetime import datetime, timedelta
import secrets
import uuid

from bot_app.config import AppConfig
from bot_app.models import (
    ApprovalTask,
    ApprovalTaskKind,
    ApprovalTaskStatus,
    IncomingGroupMessage,
    ParsedCandidate,
    ResolvedSlot,
    SwapWatchRule,
)
from bot_app.storage import JsonStateStore

TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class ApprovalService:
    def __init__(self, config: AppConfig, store: JsonStateStore) -> None:
        self.config = config
        self.store = store

    def generate_token(self, length: int = 6) -> str:
        return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(length))

    async def create_task(
        self,
        incoming: IncomingGroupMessage,
        parsed: ParsedCandidate,
        group_name: str,
        slot_date: str | None = None,
    ) -> ApprovalTask:
        now = datetime.now()
        task = ApprovalTask(
            task_id=str(uuid.uuid4()),
            token=self.generate_token(),
            task_kind=ApprovalTaskKind.CLAIM,
            group_id=incoming.group_id,
            group_name=group_name,
            message_id=incoming.message_id,
            user_id=incoming.user_id,
            sender_nickname=incoming.nickname,
            raw_text=incoming.raw_text,
            campus=parsed.campus,
            slot_date=slot_date,
            start_time=parsed.start_time.strftime("%H:%M") if parsed.start_time else None,
            end_time=parsed.end_time.strftime("%H:%M") if parsed.end_time else None,
            reason=parsed.reason,
            source_timestamp=incoming.timestamp,
            created_at=now,
            expires_at=now + timedelta(seconds=self.config.approval_ttl_seconds),
        )
        await self.store.create_task(task)
        return task

    async def create_swap_match_task(
        self,
        incoming: IncomingGroupMessage,
        group_name: str,
        rule: SwapWatchRule,
        matched_have_slot: ResolvedSlot,
        matched_want_slot: ResolvedSlot,
        reason: str,
    ) -> ApprovalTask:
        now = datetime.now()
        task = ApprovalTask(
            task_id=str(uuid.uuid4()),
            token=self.generate_token(),
            task_kind=ApprovalTaskKind.SWAP_MATCH,
            group_id=incoming.group_id,
            group_name=group_name,
            message_id=incoming.message_id,
            user_id=incoming.user_id,
            sender_nickname=incoming.nickname,
            raw_text=incoming.raw_text,
            reply_text="1",
            campus=matched_want_slot.campus,
            start_time=matched_want_slot.start_time,
            end_time=matched_want_slot.end_time,
            reason=reason,
            source_timestamp=incoming.timestamp,
            created_at=now,
            expires_at=now + timedelta(seconds=self.config.approval_ttl_seconds),
            reply_to_message_id=incoming.message_id,
            target_user_id=incoming.user_id,
            matched_rule_id=rule.rule_id,
            matched_have_slot=matched_have_slot,
            matched_want_slot=matched_want_slot,
        )
        await self.store.create_task(task)
        return task

    async def get_by_token(self, token: str) -> ApprovalTask | None:
        await self.store.expire_overdue_tasks()
        return await self.store.get_task_by_token(token.upper())

    async def cancel(self, task: ApprovalTask) -> ApprovalTask:
        updated = task.model_copy(update={"status": ApprovalTaskStatus.CANCELLED})
        await self.store.update_task(updated)
        return updated

    async def mark_sent(self, task: ApprovalTask, sent_at: datetime | None = None) -> ApprovalTask:
        updated = task.model_copy(
            update={"status": ApprovalTaskStatus.SENT, "sent_at": sent_at or datetime.now()}
        )
        await self.store.update_task(updated)
        return updated

    async def mark_failed(self, task: ApprovalTask, reason: str) -> ApprovalTask:
        updated = task.model_copy(
            update={"status": ApprovalTaskStatus.FAILED, "failure_reason": reason}
        )
        await self.store.update_task(updated)
        return updated
