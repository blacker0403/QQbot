from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
import shutil
import sys
import types
import unittest
import uuid


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
from bot_app.models import ClaimMode, DismissedClaimSlot, IncomingGroupMessage, ParsedCandidate, ResolvedSlot
import bot_app.plugins.private_commands as private_commands
from bot_app.plugins.private_commands import _handle_stopclaim_command, _normalize_command
from bot_app.runtime import build_runtime, set_runtime
from bot_app.services.workflow import ClaimWorkflow
from bot_app.storage import JsonStateStore


class _MatchingPrefilter:
    def match(self, text: str) -> bool:
        return True


class _Parser:
    async def parse(self, text: str) -> ParsedCandidate:
        return ParsedCandidate(is_candidate=True, confidence=0.9)


class _SemanticNearStartParser:
    async def parse(self, text: str) -> ParsedCandidate:
        return ParsedCandidate(
            is_candidate=True,
            campus="jiulonghu",
            start_time=time(18, 0),
            end_time=time(19, 0),
            confidence=0.99,
        )

    def _detect_intent(self, text: str) -> bool:
        return True

    def _looks_like_question(self, text: str) -> bool:
        return False

    def _has_offer_action(self, text: str) -> bool:
        return True


class _SemanticNearStartWithoutEndParser(_SemanticNearStartParser):
    async def parse(self, text: str) -> ParsedCandidate:
        return ParsedCandidate(
            is_candidate=True,
            campus="jiulonghu",
            start_time=time(17, 0),
            end_time=None,
            confidence=0.99,
        )


class _SlotParser:
    def parse_slot(self, text: str, reference_time: datetime):
        return None


class _NearStartSlotParser:
    def parse_slot(self, text: str, reference_time: datetime):
        return ResolvedSlot(date="2026-05-01", start_time="20:30", end_time="21:30", campus="九龙湖")


class _DismissedSlotParser:
    def parse_slot(self, text: str, reference_time: datetime):
        return ResolvedSlot(date="2026-05-02", start_time="19:00", end_time="20:00", campus="jiulonghu")


class _ExchangeParser:
    def looks_like_exchange(self, text: str) -> bool:
        return False


class _Approval:
    def __init__(self) -> None:
        self.created = 0

    async def create_task(self, *args, **kwargs):
        self.created += 1
        raise AssertionError("claim task should not be created while listening is paused")


class _RecordingApproval:
    def __init__(self) -> None:
        self.created = 0

    async def create_task(self, *args, **kwargs):
        self.created += 1
        raise AssertionError("claim task should not be created for near-start slots")


class _Cooldown:
    async def get_remaining(self, now: datetime):
        raise AssertionError("cooldown should not be checked for near-start slots")


class _Notifier:
    async def send_candidate_notice(self, *args, **kwargs):
        raise AssertionError("owner should not be notified for near-start slots")

    async def send_cooldown_notice(self, *args, **kwargs):
        raise AssertionError("owner should not receive cooldown notice for near-start slots")


class _FixedStopClaimDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 5, 13, 14, 45, 0)
        if tz is not None:
            return value.replace(tzinfo=tz)
        return value


class _PrivateBot:
    def __init__(self) -> None:
        self.private_messages: list[dict] = []

    async def send_private_msg(self, **kwargs):
        self.private_messages.append(dict(kwargs))
        return {"message_id": len(self.private_messages)}

    async def delete_msg(self, **kwargs):
        return {}


def _make_test_store(name: str) -> tuple[JsonStateStore, Path]:
    data_dir = Path(__file__).resolve().parents[1] / "data" / f"test-{name}-{uuid.uuid4().hex}"
    return JsonStateStore(str(data_dir / "state.json")), data_dir


class ClaimListeningPauseTest(unittest.IsolatedAsyncioTestCase):
    def test_pause_command_alias_maps_to_listen_pause(self) -> None:
        for text in ("暂停监听", "暂停监听送场地"):
            with self.subTest(text=text):
                command = _normalize_command(text)

                self.assertIsNotNone(command)
                assert command is not None
                self.assertEqual(command.name, "listen")
                self.assertEqual(command.args, ["pause"])

    def test_stopclaim_command_alias_maps_to_stopclaim(self) -> None:
        for text in ("/stopclaim 4-5", "/停止抢送 4-5"):
            with self.subTest(text=text):
                command = _normalize_command(text)

                self.assertIsNotNone(command)
                assert command is not None
                self.assertEqual(command.name, "stopclaim")
                self.assertEqual(command.args, ["4-5"])

    async def test_stopclaim_command_saves_today_dismissed_slot_until_midnight(self) -> None:
        data_dir = Path(__file__).resolve().parents[1] / "data" / f"test-stopclaim-{uuid.uuid4().hex}"
        data_dir.mkdir(parents=True)
        original_datetime = private_commands.datetime
        try:
            private_commands.datetime = _FixedStopClaimDateTime
            runtime = build_runtime(
                AppConfig(
                    owner_qq="10000",
                    target_groups=["target-group"],
                    target_campus_key="jiulonghu",
                    campus_aliases={"jiulonghu": ["jiulonghu"]},
                    storage_path=str(data_dir / "state.json"),
                )
            )
            set_runtime(runtime)
            bot = _PrivateBot()

            await _handle_stopclaim_command(bot, "10000", ["4-5"])

            self.assertTrue(
                await runtime.store.is_dismissed_claim_slot(
                    ResolvedSlot(date="2026-05-13", start_time="16:00", end_time="17:00", campus="jiulonghu"),
                    now=datetime(2026, 5, 13, 14, 45, 0),
                )
            )
            self.assertFalse(
                await runtime.store.is_dismissed_claim_slot(
                    ResolvedSlot(date="2026-05-13", start_time="16:00", end_time="17:00", campus="jiulonghu"),
                    now=datetime(2026, 5, 14, 0, 0, 1),
                )
            )
            self.assertIn("16:00-17:00", bot.private_messages[-1]["message"])
        finally:
            private_commands.datetime = original_datetime
            shutil.rmtree(data_dir, ignore_errors=True)

    async def test_paused_claim_listening_ignores_offer_messages(self) -> None:
        store, data_dir = _make_test_store("listening-pause")
        try:
            store._state.claim_listening_paused = True
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
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    async def test_near_start_claim_is_ignored_without_owner_notice(self) -> None:
        store, data_dir = _make_test_store("near-start-manual")
        try:
            store._state.claim_mode = ClaimMode.MANUAL
            approval = _RecordingApproval()
            workflow = ClaimWorkflow(
                config=AppConfig(
                    owner_qq="10000",
                    target_groups=["target-group"],
                    target_campus_key="jiulonghu",
                    campus_aliases={"jiulonghu": ["九龙湖"]},
                ),
                store=store,
                prefilter=_MatchingPrefilter(),
                slot_parser=_NearStartSlotParser(),
                exchange_parser=_ExchangeParser(),
                parser=_Parser(),
                approval=approval,
                cooldown=_Cooldown(),
                notifier=_Notifier(),
            )

            await workflow.handle_group_message(
                bot=types.SimpleNamespace(
                    self_id="20000",
                    get_group_info=lambda **kwargs: (_ for _ in ()).throw(
                        AssertionError("group name should not be fetched for near-start slots")
                    ),
                ),
                incoming=IncomingGroupMessage(
                    group_id="target-group",
                    user_id="30000",
                    message_id="m2",
                    nickname="sender",
                    raw_text="今晚20:30-21:30送九龙湖",
                    timestamp=datetime(2026, 5, 1, 20, 0, 1),
                ),
            )

            self.assertEqual(approval.created, 0)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    async def test_near_start_auto_claim_does_not_send_group_message(self) -> None:
        store, data_dir = _make_test_store("near-start-auto")
        try:
            store._state.claim_mode = ClaimMode.AUTO
            approval = _RecordingApproval()
            workflow = ClaimWorkflow(
                config=AppConfig(
                    owner_qq="10000",
                    target_groups=["target-group"],
                    target_campus_key="jiulonghu",
                    campus_aliases={"jiulonghu": ["九龙湖"]},
                ),
                store=store,
                prefilter=_MatchingPrefilter(),
                slot_parser=_NearStartSlotParser(),
                exchange_parser=_ExchangeParser(),
                parser=_Parser(),
                approval=approval,
                cooldown=_Cooldown(),
                notifier=_Notifier(),
            )

            async def send_group_msg(**kwargs):
                raise AssertionError("bot should not send 1 for near-start slots")

            await workflow.handle_group_message(
                bot=types.SimpleNamespace(self_id="20000", send_group_msg=send_group_msg),
                incoming=IncomingGroupMessage(
                    group_id="target-group",
                    user_id="30000",
                    message_id="m3",
                    nickname="sender",
                    raw_text="今晚20:30-21:30送九龙湖",
                    timestamp=datetime(2026, 5, 1, 20, 0, 1),
                ),
            )

            self.assertEqual(approval.created, 0)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


    async def test_near_start_auto_claim_uses_semantic_slot_when_structural_parse_is_missing(self) -> None:
        store, data_dir = _make_test_store("near-start-auto-semantic")
        try:
            store._state.claim_mode = ClaimMode.AUTO
            approval = _RecordingApproval()
            workflow = ClaimWorkflow(
                config=AppConfig(
                    owner_qq="10000",
                    target_groups=["target-group"],
                    target_campus_key="jiulonghu",
                    campus_aliases={"jiulonghu": ["jiulonghu"]},
                ),
                store=store,
                prefilter=_MatchingPrefilter(),
                slot_parser=_SlotParser(),
                exchange_parser=_ExchangeParser(),
                parser=_SemanticNearStartParser(),
                approval=approval,
                cooldown=_Cooldown(),
                notifier=_Notifier(),
            )

            async def send_group_msg(**kwargs):
                raise AssertionError("bot should not send 1 for near-start semantic slots")

            await workflow.handle_group_message(
                bot=types.SimpleNamespace(
                    self_id="20000",
                    send_group_msg=send_group_msg,
                    get_group_info=lambda **kwargs: (_ for _ in ()).throw(
                        AssertionError("group name should not be fetched for near-start semantic slots")
                    ),
                ),
                incoming=IncomingGroupMessage(
                    group_id="target-group",
                    user_id="30000",
                    message_id="m4",
                    nickname="sender",
                    raw_text="送6-7",
                    timestamp=datetime(2026, 5, 15, 17, 46, 8),
                ),
            )

            self.assertEqual(approval.created, 0)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


    async def test_near_start_auto_claim_uses_semantic_start_when_end_time_is_missing(self) -> None:
        store, data_dir = _make_test_store("near-start-auto-semantic-no-end")
        try:
            store._state.claim_mode = ClaimMode.AUTO
            approval = _RecordingApproval()
            workflow = ClaimWorkflow(
                config=AppConfig(
                    owner_qq="10000",
                    target_groups=["target-group"],
                    target_campus_key="jiulonghu",
                    campus_aliases={"jiulonghu": ["jiulonghu"]},
                ),
                store=store,
                prefilter=_MatchingPrefilter(),
                slot_parser=_SlotParser(),
                exchange_parser=_ExchangeParser(),
                parser=_SemanticNearStartWithoutEndParser(),
                approval=approval,
                cooldown=_Cooldown(),
                notifier=_Notifier(),
            )

            async def send_group_msg(**kwargs):
                raise AssertionError("bot should not send 1 for near-start semantic slots without end time")

            await workflow.handle_group_message(
                bot=types.SimpleNamespace(
                    self_id="20000",
                    send_group_msg=send_group_msg,
                    get_group_info=lambda **kwargs: (_ for _ in ()).throw(
                        AssertionError("group name should not be fetched for near-start semantic slots")
                    ),
                ),
                incoming=IncomingGroupMessage(
                    group_id="target-group",
                    user_id="30000",
                    message_id="m5",
                    nickname="sender",
                    raw_text="有人送",
                    timestamp=datetime(2026, 5, 13, 16, 30, 0),
                ),
            )

            self.assertEqual(approval.created, 0)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


    async def test_dismissed_claim_slot_is_ignored_until_midnight(self) -> None:
        store, data_dir = _make_test_store("dismissed-slot")
        try:
            now = datetime(2026, 5, 1, 20, 0, 0)
            expires_at = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
            dismissed_slot = DismissedClaimSlot(
                date="2026-05-02",
                start_time="19:00",
                end_time="20:00",
                campus="jiulonghu",
                created_at=now,
                expires_at=expires_at,
            )
            await store.save_dismissed_claim_slot(dismissed_slot, now=now)
            store._state.claim_mode = ClaimMode.AUTO
            approval = _RecordingApproval()
            workflow = ClaimWorkflow(
                config=AppConfig(
                    owner_qq="10000",
                    target_groups=["target-group"],
                    target_campus_key="jiulonghu",
                    campus_aliases={"jiulonghu": ["jiulonghu"]},
                ),
                store=store,
                prefilter=_MatchingPrefilter(),
                slot_parser=_DismissedSlotParser(),
                exchange_parser=_ExchangeParser(),
                parser=_Parser(),
                approval=approval,
                cooldown=_Cooldown(),
                notifier=_Notifier(),
            )

            async def fail_group_msg(**kwargs):
                raise AssertionError("bot should not send 1 for dismissed slots")

            await workflow.handle_group_message(
                bot=types.SimpleNamespace(
                    self_id="20000",
                    send_group_msg=fail_group_msg,
                    get_group_info=lambda **kwargs: (_ for _ in ()).throw(
                        AssertionError("group name should not be fetched for dismissed slots")
                    ),
                ),
                incoming=IncomingGroupMessage(
                    group_id="target-group",
                    user_id="30000",
                    message_id="m4",
                    nickname="sender",
                    raw_text="tomorrow 19:00-20:00 offer",
                    timestamp=now,
                ),
            )

            self.assertEqual(approval.created, 0)
            self.assertFalse(
                await store.is_dismissed_claim_slot(
                    ResolvedSlot(date="2026-05-02", start_time="19:00", end_time="20:00", campus="jiulonghu"),
                    now=expires_at + timedelta(seconds=1),
                )
            )
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
