from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

import pytest

from bot_app.config import AppConfig
from bot_app.models import ApprovalTask, ApprovalTaskStatus, CourtHandoffStatus
from bot_app.services.court_handoff import CourtHandoffService
from bot_app.storage import JsonStateStore


def _sent_claim(task_id: str = "task-1", peer_qq: str = "20000") -> ApprovalTask:
    now = datetime(2026, 10, 18, 15, 0)
    return ApprovalTask(
        task_id=task_id,
        token="ABC123",
        group_id="30000",
        group_name="target group",
        message_id="message-1",
        user_id=peer_qq,
        sender_nickname="sender",
        raw_text="送明天67场地",
        slot_date="2026-10-19",
        start_time="18:00",
        end_time="19:00",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status=ApprovalTaskStatus.SENT,
        sent_at=now,
    )


def _service(directory: str) -> tuple[CourtHandoffService, JsonStateStore]:
    config = AppConfig(owner_qq="10000", storage_path=str(Path(directory) / "state.json"))
    store = JsonStateStore(config.storage_path)
    return CourtHandoffService(config, store, minimax=None), store


@pytest.mark.asyncio
async def test_existing_friend_completes_text_handoff() -> None:
    with TemporaryDirectory() as directory:
        service, store = _service(directory)
        bot = AsyncMock()
        bot.self_id = "99999"
        bot.get_friend_list.return_value = [{"user_id": 20000}]

        await service.start(bot, _sent_claim())
        await service.handle_private_message(
            bot,
            user_id="20000",
            text="位置：桃园体育馆A区3号场\n一卡通号：12345678",
            image_candidates=[],
        )

        assert [call.kwargs for call in bot.send_private_msg.await_args_list] == [
            {"user_id": 20000, "message": "同学你好"},
            {"user_id": 20000, "message": "谢谢"},
            {"user_id": 10000, "message": "2026-10-19 18:00至19:00-桃园体育馆A区3号场-12345678"},
        ]
        handoff = await store.get_court_handoff()
        assert handoff is not None
        assert handoff.status == CourtHandoffStatus.COMPLETED


@pytest.mark.asyncio
async def test_waits_for_expected_friend_request_and_uses_default_settings() -> None:
    with TemporaryDirectory() as directory:
        service, store = _service(directory)
        bot = AsyncMock()
        bot.self_id = "99999"
        bot.get_friend_list.return_value = []

        await service.start(bot, _sent_claim())

        assert bot.send_private_msg.await_count == 0
        assert await service.handle_friend_request(bot, "other-user", "other-flag") is False
        assert bot.set_friend_add_request.await_count == 0

        assert await service.handle_friend_request(bot, "20000", "expected-flag") is True

        bot.set_friend_add_request.assert_awaited_once_with(
            flag="expected-flag",
            approve=True,
        )
        bot.send_private_msg.assert_awaited_once_with(user_id=20000, message="同学你好")
        handoff = await store.get_court_handoff()
        assert handoff is not None
        assert handoff.status == CourtHandoffStatus.WAITING_DETAILS


@pytest.mark.asyncio
async def test_image_ocr_can_complete_handoff() -> None:
    with TemporaryDirectory() as directory:
        service, _store = _service(directory)
        bot = AsyncMock()
        bot.self_id = "99999"
        bot.get_friend_list.return_value = [{"user_id": 20000}]
        bot.call_api.return_value = {
            "texts": [
                {"text": "位置：九龙湖体育馆2楼6号场"},
                {"text": "一卡通号：87654321"},
            ]
        }

        await service.start(bot, _sent_claim())
        await service.handle_private_message(
            bot,
            user_id="20000",
            text="",
            image_candidates=[["image-file-id", "https://example.invalid/image.jpg"]],
        )

        bot.call_api.assert_awaited_once_with("ocr_image", image="image-file-id")
        assert bot.send_private_msg.await_args_list[-1].kwargs == {
            "user_id": 10000,
            "message": "2026-10-19 18:00至19:00-九龙湖体育馆2楼6号场-87654321",
        }


@pytest.mark.asyncio
async def test_split_details_survive_restart_without_duplicate_greeting() -> None:
    with TemporaryDirectory() as directory:
        service, _store = _service(directory)
        bot = AsyncMock()
        bot.self_id = "99999"
        bot.get_friend_list.return_value = [{"user_id": 20000}]

        await service.start(bot, _sent_claim())
        await service.handle_private_message(
            bot,
            user_id="20000",
            text="位置：橘园体育馆8号场",
            image_candidates=[],
        )

        restarted_service, restarted_store = _service(directory)
        await restarted_service.resume(bot)
        await restarted_service.handle_private_message(
            bot,
            user_id="20000",
            text="一卡通号：11223344",
            image_candidates=[],
        )

        messages = [call.kwargs["message"] for call in bot.send_private_msg.await_args_list]
        assert messages.count("同学你好") == 1
        assert messages.count("谢谢") == 1
        handoff = await restarted_store.get_court_handoff()
        assert handoff is not None
        assert handoff.status == CourtHandoffStatus.COMPLETED


@pytest.mark.asyncio
async def test_first_active_handoff_is_not_replaced() -> None:
    with TemporaryDirectory() as directory:
        service, store = _service(directory)
        bot = AsyncMock()
        bot.self_id = "99999"
        bot.get_friend_list.return_value = []

        await service.start(bot, _sent_claim(task_id="first", peer_qq="20000"))
        await service.start(bot, _sent_claim(task_id="second", peer_qq="30000"))

        handoff = await store.get_court_handoff()
        assert handoff is not None
        assert handoff.task_id == "first"
        assert handoff.peer_qq == "20000"
