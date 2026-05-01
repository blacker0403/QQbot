from __future__ import annotations

from datetime import datetime, time
from enum import Enum

from pydantic import BaseModel, Field


class IncomingGroupMessage(BaseModel):
    group_id: str
    user_id: str
    message_id: str
    nickname: str
    raw_text: str
    timestamp: datetime


class ParsedCandidate(BaseModel):
    is_candidate: bool = False
    campus: str | None = None
    start_time: time | None = None
    end_time: time | None = None
    confidence: float = 0.0
    reason: str = ""
    needs_llm: bool = False


class ApprovalTaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    CANCELLED = "cancelled"
    SENT = "sent"
    EXPIRED = "expired"
    FAILED = "failed"


class ApprovalTaskKind(str, Enum):
    CLAIM = "claim"
    SWAP_MATCH = "swap_match"


class ClaimMode(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


class ResolvedSlot(BaseModel):
    date: str
    start_time: str
    end_time: str | None = None
    campus: str
    match_mode: str = "exact"
    raw_text: str = ""


class SwapWatchRule(BaseModel):
    rule_id: str
    name: str
    have_slots: list[ResolvedSlot] = Field(default_factory=list)
    want_slots: list[ResolvedSlot] = Field(default_factory=list)
    created_at: datetime
    enabled: bool = True


class ApprovalTask(BaseModel):
    task_id: str
    token: str
    task_kind: ApprovalTaskKind = ApprovalTaskKind.CLAIM
    group_id: str
    group_name: str
    message_id: str
    user_id: str
    sender_nickname: str
    raw_text: str
    reply_text: str = "1"
    campus: str | None = None
    slot_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    reason: str = ""
    source_timestamp: datetime | None = None
    created_at: datetime
    expires_at: datetime
    status: ApprovalTaskStatus = ApprovalTaskStatus.PENDING
    sent_at: datetime | None = None
    failure_reason: str | None = None
    reply_to_message_id: str | None = None
    target_user_id: str | None = None
    matched_rule_id: str | None = None
    matched_have_slot: ResolvedSlot | None = None
    matched_want_slot: ResolvedSlot | None = None


class AutoRecallTask(BaseModel):
    task_id: str
    group_id: str
    group_name: str
    sent_message_id: str
    user_id: str
    sender_nickname: str
    raw_text: str
    slot_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    sent_at: datetime


class PersistedState(BaseModel):
    last_claimed_at: datetime | None = None
    claim_mode: ClaimMode = ClaimMode.MANUAL
    claim_listening_paused: bool = False
    pending_auto_recall: AutoRecallTask | None = None
    secondary_owner_qq: str | None = None
    tasks: dict[str, ApprovalTask] = Field(default_factory=dict)
    swap_watch_rules: dict[str, SwapWatchRule] = Field(default_factory=dict)


class LLMDecision(BaseModel):
    is_real_offer: bool
    campus: str | None = None
    start_time: str | None = None
    confidence: float = 0.0
    reason: str = ""


class ExchangeParseResult(BaseModel):
    is_exchange_candidate: bool = False
    their_have_slots: list[ResolvedSlot] = Field(default_factory=list)
    their_want_slots: list[ResolvedSlot] = Field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    needs_llm: bool = False
