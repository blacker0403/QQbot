from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bot_app.config import AppConfig
from bot_app.models import ApprovalTask, ApprovalTaskStatus, IncomingGroupMessage, ParsedCandidate
from bot_app.plugins.private_commands import _is_authorized_user
from bot_app.runtime import build_runtime, set_runtime
from bot_app.services.notify import NotifyService
from bot_app.storage import JsonStateStore


def _claim_task(now: datetime) -> ApprovalTask:
    return ApprovalTask(
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


@pytest.mark.asyncio
async def test_notification_cc_receives_only_successful_claim_result() -> None:
    with TemporaryDirectory() as directory:
        config = AppConfig(
            owner_qq="10000",
            notification_cc_qqs=["20000", "30000"],
            target_groups=["973912881", "721079985"],
        )
        store = JsonStateStore(str(Path(directory) / "state.json"))
        for qq in config.notification_cc_qqs:
            await store.add_notification_cc_qq(qq)
        notifier = NotifyService(config, store)
        bot = AsyncMock()
        now = datetime(2026, 10, 18, 12, 0)
        task = _claim_task(now)

        await notifier.send_auto_claim_result(bot, task, timedelta(hours=24))

        assert [call.kwargs["user_id"] for call in bot.send_private_msg.await_args_list] == [
            10000,
            20000,
            30000,
        ]
        owner_message = bot.send_private_msg.await_args_list[0].kwargs["message"]
        cc_messages = [
            call.kwargs["message"] for call in bot.send_private_msg.await_args_list[1:]
        ]
        assert "仅供查看，无法操作机器人" not in owner_message
        assert all("回复 0" not in message for message in cc_messages)
        assert all("仅供查看，无法操作机器人" in message for message in cc_messages)

        bot.reset_mock()
        incoming = IncomingGroupMessage(
            group_id=task.group_id,
            user_id=task.user_id,
            message_id=task.message_id,
            nickname=task.sender_nickname,
            raw_text=task.raw_text,
            timestamp=now,
        )
        await notifier.send_candidate_notice(bot, incoming, ParsedCandidate(), task)

        assert [call.kwargs["user_id"] for call in bot.send_private_msg.await_args_list] == [10000]

        bot.reset_mock()
        await notifier.send_confirm_result(bot, task)

        assert [call.kwargs["user_id"] for call in bot.send_private_msg.await_args_list] == [
            10000,
            20000,
            30000,
        ]


@pytest.mark.asyncio
async def test_notification_cc_has_no_command_permission() -> None:
    with TemporaryDirectory() as directory:
        runtime = build_runtime(
            AppConfig(
                owner_qq="10000",
                notification_cc_qqs=["20000"],
                storage_path=str(Path(directory) / "state.json"),
            )
        )
        set_runtime(runtime)

        assert await _is_authorized_user("10000") is True
        assert await _is_authorized_user("20000") is False


@pytest.mark.asyncio
async def test_notification_cc_limit_is_five_excluding_owner() -> None:
    with TemporaryDirectory() as directory:
        store = JsonStateStore(str(Path(directory) / "state.json"))
        for index in range(5):
            assert await store.add_notification_cc_qq(f"2000{index}") is True

        with pytest.raises(ValueError, match="limit"):
            await store.add_notification_cc_qq("20005")

    with pytest.raises(ValidationError, match="at most 5"):
        AppConfig(
            owner_qq="10000",
            notification_cc_qqs=[f"2000{index}" for index in range(6)],
        )

    with pytest.raises(ValidationError, match="must not include owner_qq"):
        AppConfig(owner_qq="10000", notification_cc_qqs=["10000"])


@pytest.mark.asyncio
async def test_legacy_secondary_owner_is_migrated_to_notification_cc() -> None:
    with TemporaryDirectory() as directory:
        state_path = Path(directory) / "state.json"
        state_path.write_text(json.dumps({"secondary_owner_qq": "20000"}), encoding="utf-8")

        store = JsonStateStore(str(state_path))

        assert await store.get_notification_cc_qqs() == ["20000"]
        saved_state = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved_state["notification_cc_qqs"] == ["20000"]
        assert "secondary_owner_qq" not in saved_state
