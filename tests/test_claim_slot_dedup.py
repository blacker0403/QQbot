from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from test_claim_listening_pause import _GroupBot
from bot_app.config import AppConfig
from bot_app.models import ClaimMode, IncomingGroupMessage, ParsedCandidate, TemporaryClaimSlot
from bot_app.plugins.private_commands import _confirm_task
from bot_app.runtime import build_runtime, set_runtime
from bot_app.services.notify import NotifyService


class ClaimSlotDedupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = AppConfig(
            owner_qq="10000",
            target_groups=["973912881", "721079985"],
            target_campus_key="jiulonghu",
            campus_aliases={"jiulonghu": ["九龙湖"]},
            storage_path=str(Path(self.directory.name) / "state.json"),
        )
        self.runtime = self.make_runtime()
        self.bot = _GroupBot()
        self.now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        original_sleep = asyncio.sleep

        async def yield_once(*args):
            await original_sleep(0)

        sleep_patch = patch("bot_app.services.workflow.asyncio.sleep", new=yield_once)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        original_send = self.bot.send_group_msg

        async def send_with_yield(**kwargs):
            await original_sleep(0)
            return await original_send(**kwargs)

        self.bot.send_group_msg = send_with_yield

    def make_runtime(self):
        runtime = build_runtime(self.config)
        runtime.notifier = AsyncMock(spec=NotifyService)
        runtime.workflow.notifier = runtime.notifier
        runtime.workflow.parser.verify_offer_with_llm = AsyncMock(return_value=True)
        set_runtime(runtime)
        return runtime

    def incoming(self, group: str, message: str, day: str = "今天", hours: str = "18:00-19:00"):
        return IncomingGroupMessage(
            group_id=group, user_id="30000", message_id=message, nickname="sender",
            raw_text=f"送{day}{hours}九龙湖场地", timestamp=self.now,
        )

    async def claim(self, incoming):
        await self.runtime.workflow.handle_group_message(self.bot, incoming)

    async def enable_temporary(self):
        await self.runtime.store.set_claim_mode(ClaimMode.AUTO)
        await self.runtime.store.save_temporary_claim_slot(TemporaryClaimSlot(
            date=self.now.date().isoformat(), start_time="18:00", end_time="19:00",
            campus="九龙湖", created_at=self.now, expires_at=self.now + timedelta(days=1),
        ), now=self.now)

    async def test_cross_group_duplicate_survives_restart_and_cooldown_reset(self):
        await self.runtime.store.set_claim_mode(ClaimMode.AUTO)
        await self.claim(self.incoming("973912881", "first"))
        self.runtime = self.make_runtime()
        await self.runtime.cooldown.reset()
        await self.claim(self.incoming("721079985", "duplicate"))
        self.assertEqual(len(self.bot.group_messages), 1)

    async def test_concurrent_temporary_claims_only_send_once(self):
        await self.enable_temporary()
        await asyncio.gather(
            self.claim(self.incoming("973912881", "first")),
            self.claim(self.incoming("721079985", "duplicate")),
        )
        self.assertEqual(len(self.bot.group_messages), 1)

    async def test_send_failure_allows_retry(self):
        await self.enable_temporary()
        with patch.object(self.bot, "send_group_msg", new=AsyncMock(side_effect=RuntimeError("send failed"))):
            await self.claim(self.incoming("973912881", "failed"))
        await self.claim(self.incoming("721079985", "retry"))
        self.assertEqual(len(self.bot.group_messages), 1)

    async def test_llm_rejection_allows_retry(self):
        await self.runtime.store.set_claim_mode(ClaimMode.AUTO)
        self.runtime.workflow.parser.verify_offer_with_llm.return_value = False
        await self.claim(self.incoming("973912881", "rejected"))
        self.runtime.workflow.parser.verify_offer_with_llm.return_value = True
        await self.claim(self.incoming("721079985", "retry"))
        self.assertEqual([message["message"] for message in self.bot.group_messages], ["1", "抱歉，看错了", "1"])

    async def test_non_target_group_is_ignored(self):
        await self.runtime.store.set_claim_mode(ClaimMode.AUTO)
        await self.claim(self.incoming("766646988", "ignored"))
        self.assertEqual(self.bot.group_messages, [])

    async def test_global_cooldown_still_blocks_other_slots(self):
        await self.runtime.store.set_claim_mode(ClaimMode.AUTO)
        await self.claim(self.incoming("973912881", "first"))
        await self.claim(self.incoming("721079985", "other", hours="20:00-21:00"))
        self.assertEqual(len(self.bot.group_messages), 1)
        self.assertTrue(await self.runtime.cooldown.is_active(self.now + timedelta(hours=23)))
        self.assertFalse(await self.runtime.cooldown.is_active(self.now + timedelta(hours=24)))

    async def test_different_dates_and_times_are_not_duplicates(self):
        await self.runtime.store.set_claim_mode(ClaimMode.AUTO)
        await self.claim(self.incoming("973912881", "first"))
        await self.runtime.cooldown.reset()
        await self.claim(self.incoming("721079985", "other-time", hours="20:00-21:00"))
        await self.runtime.cooldown.reset()
        await self.claim(self.incoming("721079985", "other-day", day="明天"))
        self.assertEqual(len(self.bot.group_messages), 3)

    async def test_manual_confirmation_deduplicates_across_groups(self):
        parsed = ParsedCandidate(is_candidate=True, campus="九龙湖", start_time=time(18), end_time=time(19))
        tasks = [await self.runtime.approval.create_task(
            self.incoming(group, str(index)), parsed, group, slot_date=self.now.date().isoformat(),
        ) for index, group in enumerate(self.config.target_groups)]
        await asyncio.gather(*(_confirm_task(self.bot, "10000", task.token) for task in tasks))
        self.assertEqual(len(self.bot.group_messages), 1)


if __name__ == "__main__":
    unittest.main()
