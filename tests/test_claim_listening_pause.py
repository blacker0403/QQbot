from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import tempfile
import types
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", dotenv)

yaml = types.ModuleType("yaml")
yaml.safe_load = lambda *args, **kwargs: {}
sys.modules.setdefault("yaml", yaml)

class _Matcher:
    def handle(self):
        def decorator(func):
            return func

        return decorator


nonebot = types.ModuleType("nonebot")
nonebot.on_message = lambda *args, **kwargs: _Matcher()
sys.modules.setdefault("nonebot", nonebot)

onebot_v11 = types.ModuleType("nonebot.adapters.onebot.v11")
onebot_v11.Bot = object
onebot_v11.GroupMessageEvent = object
onebot_v11.Message = object
onebot_v11.MessageSegment = object
onebot_v11.PrivateMessageEvent = object
sys.modules.setdefault("nonebot.adapters.onebot.v11", onebot_v11)

from bot_app.config import AppConfig
from bot_app.models import IncomingGroupMessage, ParsedCandidate
from bot_app.plugins.private_commands import _normalize_command
from bot_app.services.workflow import ClaimWorkflow
from bot_app.storage import JsonStateStore


class _MatchingPrefilter:
    def match(self, text: str) -> bool:
        return True


class _Parser:
    async def parse(self, text: str) -> ParsedCandidate:
        return ParsedCandidate(is_candidate=True, confidence=0.9)


class _SlotParser:
    def parse_slot(self, text: str, reference_time: datetime):
        return None


class _ExchangeParser:
    def looks_like_exchange(self, text: str) -> bool:
        return False


class _Approval:
    def __init__(self) -> None:
        self.created = 0

    async def create_task(self, *args, **kwargs):
        self.created += 1
        raise AssertionError("claim task should not be created while listening is paused")


class _Cooldown:
    pass


class _Notifier:
    pass


class ClaimListeningPauseTest(unittest.IsolatedAsyncioTestCase):
    def test_pause_command_alias_maps_to_listen_pause(self) -> None:
        for text in ("暂停监听", "暂停监听送场地"):
            with self.subTest(text=text):
                command = _normalize_command(text)

                self.assertIsNotNone(command)
                assert command is not None
                self.assertEqual(command.name, "listen")
                self.assertEqual(command.args, ["pause"])

    async def test_paused_claim_listening_ignores_offer_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = JsonStateStore(str(Path(tmp_dir) / "state.json"))
            await store.set_claim_listening_paused(True)
            approval = _Approval()
            workflow = ClaimWorkflow(
                config=AppConfig(
                    owner_qq="10000",
                    target_groups=["target-group"],
                    target_campus_key="jiulonghu",
                    campus_aliases={"jiulonghu": ["九龙湖"]},
                ),
                store=store,
                prefilter=_MatchingPrefilter(),
                slot_parser=_SlotParser(),
                exchange_parser=_ExchangeParser(),
                parser=_Parser(),
                approval=approval,
                cooldown=_Cooldown(),
                notifier=_Notifier(),
            )

            await workflow.handle_group_message(
                bot=types.SimpleNamespace(self_id="20000"),
                incoming=IncomingGroupMessage(
                    group_id="target-group",
                    user_id="30000",
                    message_id="m1",
                    nickname="sender",
                    raw_text="送今晚8-9九龙湖",
                    timestamp=datetime(2026, 5, 1, 20, 0, 0),
                ),
            )

            self.assertEqual(approval.created, 0)


if __name__ == "__main__":
    unittest.main()
