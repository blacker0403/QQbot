from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

import pytest

from bot_app.config import AppConfig
from bot_app.models import ApprovalTask, ApprovalTaskStatus
from bot_app.services.notify import NotifyService
from bot_app.storage import JsonStateStore


@pytest.mark.asyncio
async def test_switched_secondary_owner_receives_auto_claims_from_every_target_group() -> None:
    with TemporaryDirectory() as directory:
        config = AppConfig(
            owner_qq="10000",
            secondary_owner_qq="20000",
            target_groups=["973912881", "721079985"],
        )
        store = JsonStateStore(str(Path(directory) / "state.json"))
        await store.set_secondary_owner_qq("1978137143")
        notifier = NotifyService(config, store)
        bot = AsyncMock()
        now = datetime(2026, 10, 14, 12, 0)
        task = ApprovalTask(
            task_id="task-1",
            token="123456",
            group_id="721079985",
            group_name="second target group",
            message_id="message-1",
            user_id="30000",
            sender_nickname="sender",
            raw_text="send 6-7",
            created_at=now,
            expires_at=now + timedelta(minutes=15),
            status=ApprovalTaskStatus.SENT,
            sent_at=now,
        )

        await notifier.send_auto_claim_result(bot, task, timedelta(hours=24))

        assert [call.kwargs["user_id"] for call in bot.send_private_msg.await_args_list] == [
            10000,
            1978137143,
        ]
