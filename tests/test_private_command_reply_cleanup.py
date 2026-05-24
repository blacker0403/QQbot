from __future__ import annotations

import pytest

import bot_app.plugins.private_commands as private_commands
from bot_app.plugins.private_commands import _delete_command_reply, _reply_to_user


class FakeBot:
    def __init__(self) -> None:
        self.private_messages: list[dict] = []
        self.deleted_messages: list[int | str] = []

    async def send_private_msg(self, **kwargs):
        self.private_messages.append(dict(kwargs))
        return {"message_id": "12345"}

    async def delete_msg(self, **kwargs):
        self.deleted_messages.append(kwargs["message_id"])
        return {}


@pytest.mark.asyncio
async def test_private_command_reply_is_deleted_after_twenty_seconds(monkeypatch) -> None:
    bot = FakeBot()
    scheduled: list[tuple[object, int | str]] = []

    def fake_schedule(reply_bot, message_id):
        scheduled.append((reply_bot, message_id))

    monkeypatch.setattr(private_commands, "_schedule_command_reply_deletion", fake_schedule)

    await _reply_to_user(bot, "11111", "ok")

    assert bot.private_messages == [{"user_id": 11111, "message": "ok"}]
    assert scheduled == [(bot, "12345")]


@pytest.mark.asyncio
async def test_command_reply_deletion_is_scheduled_after_twenty_seconds(monkeypatch) -> None:
    bot = FakeBot()
    calls: list[tuple[float, object]] = []

    class FakeLoop:
        def call_later(self, delay, callback):
            calls.append((delay, callback))

    monkeypatch.setattr(private_commands.asyncio, "get_running_loop", lambda: FakeLoop())

    private_commands._schedule_command_reply_deletion(bot, "12345")

    assert len(calls) == 1
    assert calls[0][0] == 20.0


@pytest.mark.asyncio
async def test_delete_command_reply_recalls_sent_message() -> None:
    bot = FakeBot()

    await _delete_command_reply(bot, "12345")

    assert bot.deleted_messages == [12345]
