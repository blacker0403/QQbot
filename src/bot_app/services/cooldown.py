from __future__ import annotations

from datetime import datetime, timedelta

from bot_app.config import AppConfig
from bot_app.storage import JsonStateStore


class CooldownService:
    def __init__(self, config: AppConfig, store: JsonStateStore) -> None:
        self.cooldown_window = timedelta(hours=24)
        self.store = store

    async def get_remaining(self, now: datetime | None = None) -> timedelta:
        current_time = now or datetime.now()
        last_claimed_at = await self.store.get_last_claimed_at()
        if last_claimed_at is None:
            return timedelta(0)
        remaining = self.cooldown_window - (current_time - last_claimed_at)
        if remaining.total_seconds() <= 0:
            return timedelta(0)
        return remaining

    async def is_active(self, now: datetime | None = None) -> bool:
        return (await self.get_remaining(now)).total_seconds() > 0

    async def mark_claimed(self, claimed_at: datetime | None = None) -> None:
        await self.store.set_last_claimed_at(claimed_at or datetime.now())

    async def reset(self) -> None:
        await self.store.clear_last_claimed_at()
