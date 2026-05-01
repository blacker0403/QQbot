from __future__ import annotations

from datetime import datetime
import logging
import os
import sys
from typing import NamedTuple
import re
import uuid

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent

from bot_app.models import ApprovalTask, ApprovalTaskKind, ApprovalTaskStatus, ClaimMode, SwapWatchRule
from bot_app.runtime import get_runtime
from bot_app.services.avatar_rotation import AvatarRotator
from bot_app.services.notify import format_slot

logger = logging.getLogger(__name__)

private_message_matcher = on_message(priority=10, block=False)


class ParsedCommand(NamedTuple):
    name: str
    args: list[str]


def _ob_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _normalize_command(text: str) -> ParsedCommand | None:
    command_text = text.strip()
    if not command_text:
        return None

    if command_text in {"/", "菜单"}:
        return ParsedCommand(name="help", args=[])

    if command_text.startswith("/"):
        command_text = command_text[1:]

    parts = command_text.split()
    if not parts:
        return None

    raw_name = parts[0].lower()
    direct_commands = {
        "暂停监听": ParsedCommand(name="listen", args=["pause"]),
        "暂停监听送场": ParsedCommand(name="listen", args=["pause"]),
        "暂停监听送场地": ParsedCommand(name="listen", args=["pause"]),
        "暂停送场": ParsedCommand(name="listen", args=["pause"]),
        "暂停送场地": ParsedCommand(name="listen", args=["pause"]),
        "恢复监听": ParsedCommand(name="listen", args=["resume"]),
        "恢复监听送场": ParsedCommand(name="listen", args=["resume"]),
        "恢复监听送场地": ParsedCommand(name="listen", args=["resume"]),
        "恢复送场": ParsedCommand(name="listen", args=["resume"]),
        "恢复送场地": ParsedCommand(name="listen", args=["resume"]),
    }
    if raw_name in direct_commands:
        return direct_commands[raw_name]

    aliases = {
        "1": "quickconfirm",
        "0": "quickcancel",
        "health": "health",
        "状态": "status",
        "status": "status",
        "帮助": "help",
        "help": "help",
        "命令": "help",
        "commands": "help",
        "pending": "pending",
        "待确认": "pending",
        "cooldown": "cooldown",
        "冷却": "cooldown",
        "mode": "mode",
        "模式": "mode",
        "listen": "listen",
        "监听": "listen",
        "resetcooldown": "resetcooldown",
        "重置冷却": "resetcooldown",
        "resetall": "resetall",
        "重置全部": "resetall",
        "secondary": "secondary",
        "swap": "swap",
        "selflearn": "selflearn",
        "avatar": "avatar",
        "头像": "avatar",
        "restart": "restart",
        "重启": "restart",
        "setsecondary": "setsecondary",
        "removesecondary": "removesecondary",
    }
    normalized = aliases.get(raw_name)
    if normalized is None:
        return None
    return ParsedCommand(name=normalized, args=parts[1:])


@private_message_matcher.handle()
async def handle_private_command(bot: Bot, event: PrivateMessageEvent) -> None:
    if not isinstance(event, (PrivateMessageEvent, GroupMessageEvent)):
        return

    runtime = get_runtime()
    sender_id = str(event.user_id)
    if not await _is_authorized_user(sender_id):
        return

    text = event.get_plaintext().strip()
    if not text:
        return

    await runtime.store.expire_overdue_tasks()
    command = _normalize_command(text)
    if command is None:
        return

    if command.name == "help":
        await _reply_to_user(bot, sender_id, _build_help_message())
        return

    if command.name == "status":
        await _reply_to_user(bot, sender_id, await _build_status_message())
        return

    if command.name == "health":
        await _reply_to_user(bot, sender_id, await _build_health_message())
        return

    if command.name == "pending":
        await _reply_to_user(bot, sender_id, await _build_pending_message())
        return

    if command.name == "cooldown":
        await _reply_to_user(bot, sender_id, await _build_cooldown_message())
        return

    if command.name == "mode":
        await _handle_mode_command(bot, sender_id, command.args)
        return

    if command.name == "listen":
        await _handle_listen_command(bot, sender_id, command.args)
        return

    if command.name == "secondary":
        await _reply_to_user(bot, sender_id, await _build_secondary_message())
        return

    if command.name == "swap":
        await _handle_swap_command(bot, sender_id, command.args)
        return

    if command.name == "swapwatch":
        await _handle_swapwatch_command(bot, sender_id, command.args)
        return

    if command.name == "testminimax":
        await _reply_to_user(bot, sender_id, await _build_minimax_probe_message())
        return

    if command.name == "selflearn":
        await _handle_selflearn_command(bot, sender_id, command.args)
        return

    if command.name == "avatar":
        await _handle_avatar_command(bot, sender_id, command.args)
        return

    if command.name == "restart":
        await _handle_restart_command(bot, sender_id)
        return

    if command.name == "confirm":
        if not command.args:
            await _reply_to_user(bot, sender_id, "确认失败：用法 /confirm <确认码>")
            return
        await _confirm_task(bot, sender_id, command.args[0].upper())
        return

    if command.name == "quickconfirm":
        token = await _resolve_single_pending_token(bot, sender_id, action="确认")
        if token is None:
            return
        await _confirm_task(bot, sender_id, token)
        return

    if command.name == "cancel":
        if not command.args:
            await _reply_to_user(bot, sender_id, "取消失败：用法 /cancel <确认码>")
            return
        await _cancel_task(bot, sender_id, command.args[0].upper())
        return

    if command.name == "quickcancel":
        token = await _resolve_single_pending_token(bot, sender_id, action="取消")
        if token is None:
            if await _try_recall_latest_auto_claim(bot, sender_id):
                return
            await _reply_to_user(bot, sender_id, "取消失败：当前没有待确认任务，也没有可撤回的自动发送")
            return
        await _cancel_task(bot, sender_id, token)
        return

    if command.name == "resetcooldown":
        await runtime.cooldown.reset()
        await _reply_to_user(bot, sender_id, "已重置自动模式冷却")
        return

    if command.name == "resetpending":
        cancelled = await runtime.store.cancel_pending_tasks()
        await _reply_to_user(bot, sender_id, f"已重置：已取消 {len(cancelled)} 个待确认任务")
        return

    if command.name == "resetall":
        cancelled = await runtime.store.cancel_pending_tasks()
        await runtime.cooldown.reset()
        await _reply_to_user(bot, sender_id, f"已重置：已取消 {len(cancelled)} 个待确认任务")
        return

    if command.name == "setsecondary":
        if sender_id != runtime.config.owner_qq:
            await _reply_to_user(bot, sender_id, "只有第一主人可以设置第二主人")
            return
        if not command.args:
            await _reply_to_user(bot, sender_id, "用法：/setsecondary <QQ号>")
            return
        secondary_owner_qq = command.args[0].strip()
        if not secondary_owner_qq.isdigit():
            await _reply_to_user(bot, sender_id, "第二主人 QQ 号必须是纯数字")
            return
        if secondary_owner_qq == runtime.config.owner_qq:
            await _reply_to_user(bot, sender_id, "第一主人和第二主人不能相同")
            return
        await runtime.store.set_secondary_owner_qq(secondary_owner_qq)
        await _reply_to_user(bot, sender_id, f"已设置第二主人：{secondary_owner_qq}")
        return

    if command.name == "removesecondary":
        if sender_id != runtime.config.owner_qq:
            await _reply_to_user(bot, sender_id, "只有第一主人可以移除第二主人")
            return
        secondary_owner_qq = await runtime.store.get_secondary_owner_qq()
        if not secondary_owner_qq:
            await _reply_to_user(bot, sender_id, "当前没有第二主人")
            return
        await runtime.store.set_secondary_owner_qq(None)
        await _reply_to_user(bot, sender_id, f"已移除第二主人：{secondary_owner_qq}")
        return


async def _reply_to_user(bot: Bot, user_id: str, message: str) -> None:
    await bot.send_private_msg(user_id=_ob_id(user_id), message=message)


def _restart_process() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])


async def _handle_restart_command(bot: Bot, sender_id: str, restart_process=_restart_process) -> None:
    await _reply_to_user(bot, sender_id, "正在重启 bot...")
    restart_process()


def _format_remaining_text(total_seconds: float) -> str:
    whole_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟{seconds}秒"
    return f"{seconds}秒"


async def _handle_mode_command(bot: Bot, sender_id: str, args: list[str]) -> None:
    runtime = get_runtime()
    if not args:
        await _reply_to_user(bot, sender_id, await _build_mode_message())
        return

    raw_mode = args[0].strip().lower()
    mode_map = {
        "manual": ClaimMode.MANUAL,
        "手动": ClaimMode.MANUAL,
        "auto": ClaimMode.AUTO,
        "自动": ClaimMode.AUTO,
    }
    mode = mode_map.get(raw_mode)
    if mode is None:
        await _reply_to_user(bot, sender_id, "切换失败：用法 /mode manual 或 /mode auto")
        return

    await runtime.store.set_claim_mode(mode)
    await _reply_to_user(bot, sender_id, await _build_mode_message())


async def _handle_listen_command(bot: Bot, sender_id: str, args: list[str]) -> None:
    runtime = get_runtime()
    if not args:
        await _reply_to_user(bot, sender_id, await _build_listen_message())
        return

    action = args[0].strip().lower()
    action_map = {
        "pause": True,
        "暂停": True,
        "stop": True,
        "resume": False,
        "恢复": False,
        "start": False,
        "status": None,
        "状态": None,
    }
    paused = action_map.get(action)
    if action not in action_map:
        await _reply_to_user(bot, sender_id, "切换失败：用法 /listen pause 或 /listen resume")
        return
    if paused is not None:
        await runtime.store.set_claim_listening_paused(paused)
    await _reply_to_user(bot, sender_id, await _build_listen_message())


async def _handle_swapwatch_command(bot: Bot, sender_id: str, args: list[str]) -> None:
    runtime = get_runtime()
    if not args:
        await _reply_to_user(bot, sender_id, _build_swapwatch_help_message())
        return

    action = args[0].lower()
    if action == "list":
        await _reply_to_user(bot, sender_id, await _build_swapwatch_list_message())
        return

    if action == "clear":
        cleared = await runtime.store.clear_swap_watch_rules()
        await _reply_to_user(bot, sender_id, f"已清空换场监控规则：{cleared} 条")
        return

    if action == "remove":
        if len(args) < 2:
            await _reply_to_user(bot, sender_id, "移除失败：用法 /swapwatch remove <rule_id>")
            return
        removed = await runtime.store.remove_swap_watch_rule(args[1].strip().upper())
        if removed is None:
            await _reply_to_user(bot, sender_id, f"移除失败：未找到规则 {args[1].strip().upper()}")
            return
        await _reply_to_user(bot, sender_id, f"已移除换场监控规则：{removed.name} ({removed.rule_id})")
        return

    if action != "add":
        await _reply_to_user(bot, sender_id, _build_swapwatch_help_message())
        return

    payload = " ".join(args[1:]).strip()
    parsed = _parse_swapwatch_add_payload(payload)
    if parsed is None:
        await _reply_to_user(
            bot,
            sender_id,
            "添加失败：用法 /swapwatch add <名称> | have: <槽位1>, <槽位2> | want: <槽位1>, <槽位2>",
        )
        return

    now = datetime.now()
    name, have_text, want_text = parsed
    have_slots = runtime.slot_parser.parse_swap_rule_list(have_text, now)
    want_slots = runtime.slot_parser.parse_swap_rule_list(want_text, now)
    if not have_slots:
        await _reply_to_user(bot, sender_id, "添加失败：have 槽位未能解析出有效日期和时间范围")
        return
    if not want_slots:
        await _reply_to_user(bot, sender_id, "添加失败：want 槽位未能解析出有效日期和时间范围")
        return

    rule = SwapWatchRule(
        rule_id=uuid.uuid4().hex[:8].upper(),
        name=name,
        have_slots=have_slots,
        want_slots=want_slots,
        created_at=now,
        enabled=True,
    )
    saved_rule = await runtime.store.save_swap_watch_rule(rule, now=now)
    if saved_rule is None:
        await _reply_to_user(bot, sender_id, "添加失败：规则已过期或无有效槽位")
        return
    if saved_rule.rule_id != rule.rule_id:
        await _reply_to_user(bot, sender_id, f"已存在相同换场监控规则：{saved_rule.name} ({saved_rule.rule_id})")
        return
    await _reply_saved_swap_rule(bot, sender_id, saved_rule.rule_id, now)


async def _handle_swap_command(bot: Bot, sender_id: str, args: list[str]) -> None:
    runtime = get_runtime()
    if not args:
        await _reply_to_user(bot, sender_id, _build_swap_simple_help_message())
        return

    action = args[0].lower()
    if action == "list":
        await _reply_to_user(bot, sender_id, await _build_swapwatch_list_message())
        return

    if action in {"clear", "remove"}:
        await _reply_to_user(bot, sender_id, _build_swap_simple_help_message())
        return

    payload = " ".join(args).strip()
    now = datetime.now()
    provider = runtime.parser.minimax or runtime.exchange_parser.minimax
    parse_swap_command = getattr(provider, "parse_swap_command", None) if provider is not None else None
    if parse_swap_command is None:
        await _reply_to_user(bot, sender_id, "添加失败：/swap 自然语言识别需要可用的 MiniMax 配置")
        return

    try:
        parsed_groups = await parse_swap_command(
            text=payload,
            reference_time=now,
            target_aliases=runtime.config.target_campus_aliases,
        )
    except Exception as exc:
        logger.warning("MiniMax swap command parsing failed: %r", exc)
        parsed_groups = _parse_swap_command_fallback(payload, now)
        if not parsed_groups:
            reason = str(exc) or type(exc).__name__
            await _reply_to_user(bot, sender_id, f"添加失败：MiniMax 未能识别这条 /swap：{reason}")
            return
    if not parsed_groups:
        parsed_groups = _parse_swap_command_fallback(payload, now)
        if not parsed_groups:
            await _reply_to_user(bot, sender_id, "添加失败：MiniMax 未能从 /swap 中识别出有效 have/want 场地")
            return

    await _save_swap_rule_groups(bot, sender_id, parsed_groups, now)


async def _handle_avatar_command(bot: Bot, sender_id: str, args: list[str]) -> None:
    if args and args[0].lower() not in {"rotate", "now"}:
        await _reply_to_user(bot, sender_id, "用法：/avatar 或 /avatar rotate")
        return

    runtime = get_runtime()
    rotator = AvatarRotator(runtime.config.avatar_rotation)
    try:
        result = await rotator.rotate_once(bot)
    except Exception as exc:
        logger.warning("Manual avatar rotation failed: %r", exc)
        await _reply_to_user(bot, sender_id, f"头像更新失败：{exc}")
        return

    if result is None:
        await _reply_to_user(bot, sender_id, "头像更新失败：没有可用头像来源")
        return
    message = f"头像已更新：{result.avatar}"
    if result.nickname:
        message += f"\n昵称已更新：{result.nickname}"
    await _reply_to_user(bot, sender_id, message)


async def _handle_selflearn_command(bot: Bot, sender_id: str, args: list[str]) -> None:
    runtime = get_runtime()
    if not args:
        await _reply_to_user(bot, sender_id, _build_selflearn_help_message())
        return

    action = args[0].lower()
    if action == "preview":
        preview = runtime.self_learning.preview()
        await _reply_to_user(bot, sender_id, _format_selflearn_preview(preview))
        return

    if action == "run":
        preview = runtime.self_learning.preview()
        texts = [sample.text for sample in preview.samples]
        report = await runtime.self_learning.run_live(bot, texts)
        await _reply_to_user(bot, sender_id, _format_selflearn_report(report))
        return

    if action == "apply":
        if len(args) < 2:
            await _reply_to_user(bot, sender_id, "应用失败：用法 /selflearn apply <id>")
            return
        applied = runtime.self_learning.apply_candidate(args[1].strip().upper())
        if applied is None:
            await _reply_to_user(bot, sender_id, f"应用失败：未找到待确认规则 {args[1].strip().upper()}")
            return
        await _reply_to_user(bot, sender_id, f"已应用自学习规则：{applied.get('rule_id')} {applied.get('pattern')}")
        return

    await _reply_to_user(bot, sender_id, _build_selflearn_help_message())


def _format_selflearn_preview(preview) -> str:
    lines = ["【自学习预览】"]
    if not preview.samples:
        lines.append("没有从学习群提取到候选样本")
        return "\n".join(lines)
    for index, sample in enumerate(preview.samples, start=1):
        lines.append(f"{index}. {sample.kind} | {sample.text}")
    lines.append("运行：/selflearn run")
    return "\n".join(lines)


def _format_selflearn_report(report) -> str:
    lines = ["【自学习验证报告】"]
    if not report.results:
        lines.append("没有可验证样本")
        return "\n".join(lines)
    for result in report.results:
        status = "一致" if result.consistent else "不一致"
        lines.append(f"- {status} | {result.expected_action}->{result.actual_action} | {result.text}")
    return "\n".join(lines)


def _parse_swapwatch_add_payload(payload: str) -> tuple[str, str, str] | None:
    if "|" not in payload:
        return None
    parts = [part.strip() for part in payload.split("|")]
    if len(parts) != 3:
        return None
    name = parts[0]
    have_prefix, _, have_text = parts[1].partition(":")
    want_prefix, _, want_text = parts[2].partition(":")
    if have_prefix.strip().lower() != "have" or want_prefix.strip().lower() != "want":
        return None
    if not name or not have_text.strip() or not want_text.strip():
        return None
    return name, have_text.strip(), want_text.strip()


def _parse_swap_simple_payload(payload: str) -> tuple[str, str] | None:
    text = re.sub(r"\s+", " ", payload.strip())
    if not text:
        return None

    for parser in (
        _parse_swap_payload_have_then_want,
        _parse_swap_payload_want_then_have,
        _parse_swap_payload_by_direct_split,
    ):
        parsed = parser(text)
        if parsed is not None:
            return parsed
    return None


def _parse_swap_command_fallback(payload: str, reference_time: datetime) -> list[tuple[list, list]]:
    runtime = get_runtime()
    text = re.sub(r"\s+", " ", payload.strip().lstrip("/"))
    if text.lower().startswith("swap"):
        text = text[4:].strip()
    if not text or "换" not in text:
        return []

    have_text, _, want_text = text.partition("换")
    have_text = _strip_swap_side_prefix(have_text.strip(" ，,。.；;、"))
    want_text = _strip_swap_side_prefix(want_text.strip(" ，,。.；;、"))
    if not have_text or not want_text:
        return []

    have_slots = runtime.slot_parser.parse_swap_rule_list(have_text, reference_time)
    if not have_slots:
        return []

    fallback_date = have_slots[-1].date
    fallback_campus = have_slots[-1].campus
    want_slots = []
    for segment in _split_swap_want_options(want_text):
        parsed = runtime.slot_parser.parse_swap_rule_list(
            segment,
            reference_time,
            fallback_date=fallback_date,
            fallback_campus=fallback_campus,
        )
        if not parsed:
            return []
        want_slots.extend(parsed)
        fallback_date = parsed[-1].date
        fallback_campus = parsed[-1].campus
    if not want_slots:
        return []
    return [(have_slots, want_slots)]


def _split_swap_want_options(text: str) -> list[str]:
    return [
        part.strip(" ，,。.；;、")
        for part in re.split(r"(?:或者|或|、|/|,|，|;|；)", text)
        if part.strip(" ，,。.；;、")
    ]


def _parse_swap_payload_have_then_want(text: str) -> tuple[str, str] | None:
    have_markers = ("我手里有", "手里有", "我这边有", "我的是", "我有", "有")
    want_markers = ("我需要", "我想要", "我想换", "想换", "要换", "换到", "需要", "想要", "换")

    have_start = _find_earliest_marker(text, have_markers)
    if have_start is None:
        return None

    have_marker_index, have_marker = have_start
    search_from = have_marker_index + len(have_marker)
    want_start = _find_earliest_marker(text[search_from:], want_markers)
    if want_start is None:
        return None

    relative_index, want_marker = want_start
    want_marker_index = search_from + relative_index
    have_text = text[search_from:want_marker_index].strip(" ，,。.；;、")
    want_text = text[want_marker_index + len(want_marker):].strip(" ，,。.；;、")
    if not have_text or not want_text:
        return None
    return _strip_swap_side_prefix(have_text), _strip_swap_side_prefix(want_text)


def _parse_swap_payload_want_then_have(text: str) -> tuple[str, str] | None:
    want_markers = ("想换", "要换", "换到", "我需要", "需要", "我想要", "想要", "求", "收")
    have_markers = ("我手里有", "手里有", "我这边有", "我的是", "我有", "有")

    want_start = _find_earliest_marker(text, want_markers)
    if want_start is None:
        return None

    want_marker_index, want_marker = want_start
    want_content_start = want_marker_index + len(want_marker)
    have_start = _find_earliest_marker(text[want_content_start:], have_markers)
    if have_start is None:
        return None

    relative_index, have_marker = have_start
    have_marker_index = want_content_start + relative_index
    want_text = text[want_content_start:have_marker_index].strip(" ，,。.；;、")
    have_text = text[have_marker_index + len(have_marker):].strip(" ，,。.；;、")
    if not have_text or not want_text:
        return None
    return _strip_swap_side_prefix(have_text), _strip_swap_side_prefix(want_text)


def _parse_swap_payload_by_direct_split(text: str) -> tuple[str, str] | None:
    split_markers = ("想换", "要换", "换到", "换")
    split_marker = _find_earliest_marker(text, split_markers)
    if split_marker is None:
        return None

    split_index, marker = split_marker
    left = text[:split_index].strip(" ，,。.；;、")
    right = text[split_index + len(marker):].strip(" ，,。.；;、")
    left = _strip_swap_side_prefix(left)
    right = _strip_swap_side_prefix(right)
    if not left or not right:
        return None
    return left, right


def _find_earliest_marker(text: str, markers: tuple[str, ...]) -> tuple[int, str] | None:
    best_index = -1
    best_marker = ""
    for marker in markers:
        marker_index = text.find(marker)
        if marker_index == -1:
            continue
        if best_index == -1 or marker_index < best_index:
            best_index = marker_index
            best_marker = marker
    if best_index == -1:
        return None
    return best_index, best_marker


def _strip_swap_side_prefix(text: str) -> str:
    stripped = text.strip(" ，,。.；;、")
    prefixes = (
        "我手里有",
        "手里有",
        "我这边有",
        "我的是",
        "我有",
        "有",
        "我需要",
        "我想要",
        "我想换",
        "想换",
        "要换",
        "换到",
        "需要",
        "想要",
        "求",
        "收",
    )
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip(" ，,。.；;、")
                changed = True
    return stripped


async def _parse_swap_simple_slots(payload: str, reference_time: datetime) -> tuple[list, list]:
    runtime = get_runtime()
    parsed = _parse_swap_simple_payload(payload)
    if parsed is not None:
        have_text, want_text = parsed
        have_slots = runtime.slot_parser.parse_swap_rule_list(have_text, reference_time)
        fallback_date = have_slots[-1].date if have_slots else None
        fallback_campus = have_slots[-1].campus if have_slots else None
        want_slots = runtime.slot_parser.parse_swap_rule_list(
            want_text,
            reference_time,
            fallback_date=fallback_date,
            fallback_campus=fallback_campus,
        )
        if have_slots and want_slots:
            return have_slots, want_slots

    exchange_result = await runtime.exchange_parser.parse(payload, reference_time)
    if exchange_result.their_have_slots and exchange_result.their_want_slots:
        return exchange_result.their_have_slots, exchange_result.their_want_slots
    return [], []


async def _parse_multiple_swap_simple_slot_groups(payload: str, reference_time: datetime) -> list[tuple[list, list]]:
    segments = _split_swap_payload_into_segments(payload)
    if len(segments) <= 1:
        return []
    parsed_groups: list[tuple[list, list]] = []
    for segment in segments:
        have_slots, want_slots = await _parse_swap_simple_slots(segment, reference_time)
        if not have_slots or not want_slots:
            return []
        parsed_groups.append((have_slots, want_slots))
    return parsed_groups


def _split_swap_payload_into_segments(payload: str) -> list[str]:
    segments = [segment.strip(" ，,。.；;、") for segment in re.split(r"[，,；;]\s*", payload) if segment.strip(" ，,。.；;、")]
    if len(segments) <= 1:
        return [payload.strip()]
    valid_markers = ("换", "想换", "要换", "换到", "我需要", "需要", "我想要", "想要")
    if all(any(marker in segment for marker in valid_markers) for segment in segments):
        return segments
    return [payload.strip()]


async def _save_swap_rule_groups(
    bot: Bot,
    sender_id: str,
    slot_groups: list[tuple[list, list]],
    now: datetime,
) -> None:
    runtime = get_runtime()
    added_rules: list[SwapWatchRule] = []
    duplicate_rules: list[SwapWatchRule] = []
    expired_count = 0
    for have_slots, want_slots in slot_groups:
        rule = SwapWatchRule(
            rule_id=uuid.uuid4().hex[:8].upper(),
            name="换场需求",
            have_slots=have_slots,
            want_slots=want_slots,
            created_at=now,
            enabled=True,
        )
        saved_rule = await runtime.store.save_swap_watch_rule(rule, now=now)
        if saved_rule is None:
            expired_count += 1
            continue
        if saved_rule.rule_id != rule.rule_id:
            duplicate_rules.append(saved_rule)
            continue
        added_rules.append(saved_rule)
    if not added_rules and not duplicate_rules:
        await _reply_to_user(bot, sender_id, "添加失败：规则已过期或无有效槽位")
        return
    lines: list[str] = []
    if added_rules:
        title = "已添加换场监控规则：" if len(added_rules) == 1 else f"已添加换场监控规则：共 {len(added_rules)} 条"
        lines.append(title)
        for rule in added_rules:
            lines.append(f"- {rule.name} ({rule.rule_id})")
            lines.append(f"  have：{'; '.join(format_slot(slot) for slot in rule.have_slots)}")
            lines.append(f"  want：{'; '.join(format_slot(slot) for slot in rule.want_slots)}")
    if duplicate_rules:
        seen_rule_ids: set[str] = set()
        unique_duplicates: list[SwapWatchRule] = []
        for rule in duplicate_rules:
            if rule.rule_id in seen_rule_ids:
                continue
            seen_rule_ids.add(rule.rule_id)
            unique_duplicates.append(rule)
        lines.append(f"已存在相同规则：{len(unique_duplicates)} 条")
        for rule in unique_duplicates:
            lines.append(f"- {rule.name} ({rule.rule_id})")
    if expired_count:
        lines.append(f"未保存（已过期或无有效槽位）：{expired_count} 条")
    await _reply_to_user(bot, sender_id, "\n".join(lines))


async def _reply_saved_swap_rule(bot: Bot, sender_id: str, rule_id: str, now: datetime) -> None:
    runtime = get_runtime()
    saved_rule = await runtime.store.get_swap_watch_rule(rule_id, now=now)
    if saved_rule is None:
        await _reply_to_user(bot, sender_id, "添加失败：按时间规则更新后，have 或 want 已无可用场地")
        return
    await _reply_to_user(
        bot,
        sender_id,
        "\n".join(
            [
                f"已添加换场监控规则：{saved_rule.name} ({saved_rule.rule_id})",
                f"have：{'; '.join(format_slot(slot) for slot in saved_rule.have_slots)}",
                f"want：{'; '.join(format_slot(slot) for slot in saved_rule.want_slots)}",
            ]
        ),
    )


async def _confirm_task(bot: Bot, sender_id: str, token: str) -> None:
    runtime = get_runtime()
    task, claimed = await runtime.store.claim_pending_task_by_token(token)
    if task is None:
        await _reply_to_user(bot, sender_id, f"确认失败：未找到确认码 {token}")
        return

    if not claimed:
        if task.status in {ApprovalTaskStatus.PROCESSING, ApprovalTaskStatus.SENT}:
            await _reply_to_user(bot, sender_id, f"确认失败：确认码 {token} 已被另一主人处理")
            return
        await _reply_to_user(bot, sender_id, f"确认失败：确认码 {token} 当前状态为 {task.status}")
        return

    if task.task_kind == ApprovalTaskKind.CLAIM and runtime.workflow.has_later_group_claim(task):
        await runtime.approval.cancel(task)
        await runtime.cooldown.reset()
        await _reply_to_user(bot, sender_id, "已取消：已有其他人先扣 1，本次不再代发 1，自动冷却已重置")
        return

    try:
        await bot.send_group_msg(group_id=_ob_id(task.group_id), message=_build_group_reply_message(task))
    except Exception as exc:
        logger.exception("Failed to send claim message for token %s", token)
        await runtime.approval.mark_failed(task, str(exc))
        await _reply_to_user(bot, sender_id, f"确认失败：群消息发送失败：{exc}")
        return

    updated = await runtime.approval.mark_sent(task, datetime.now())
    await runtime.notifier.send_confirm_result(bot, updated)


async def _cancel_task(bot: Bot, sender_id: str, token: str) -> None:
    runtime = get_runtime()
    task = await runtime.approval.get_by_token(token)
    if task is None:
        await _reply_to_user(bot, sender_id, f"取消失败：未找到确认码 {token}")
        return
    if task.status != ApprovalTaskStatus.PENDING:
        await _reply_to_user(bot, sender_id, f"取消失败：确认码 {token} 当前状态为 {task.status}")
        return
    await runtime.approval.cancel(task)
    await _reply_to_user(bot, sender_id, f"已取消：确认码 {token}")


async def _try_recall_latest_auto_claim(bot: Bot, sender_id: str) -> bool:
    runtime = get_runtime()
    recall_task = await runtime.store.get_pending_auto_recall()
    if recall_task is None:
        return False
    try:
        await bot.delete_msg(message_id=int(recall_task.sent_message_id))
    except Exception as exc:
        await runtime.store.set_pending_auto_recall(None)
        await _reply_to_user(bot, sender_id, f"撤回失败：自动发送的 1 无法撤回：{exc}")
        return True

    await runtime.store.set_pending_auto_recall(None)
    await runtime.cooldown.reset()
    await runtime.notifier.send_auto_recall_result(bot, recall_task)
    return True


async def _resolve_single_pending_token(bot: Bot, sender_id: str, action: str) -> str | None:
    runtime = get_runtime()
    pending_tasks = await runtime.store.list_pending_tasks()
    if not pending_tasks:
        if action == "取消":
            return None
        await _reply_to_user(bot, sender_id, f"{action}失败：当前没有待确认任务")
        return None

    if len(pending_tasks) > 1:
        await _reply_to_user(
            bot,
            sender_id,
            f"{action}失败：当前有 {len(pending_tasks)} 个待确认任务，请先用 /pending 查看，只保留一个待确认任务后再回复 1 或 0",
        )
        return None

    return pending_tasks[0].token


async def _is_authorized_user(sender_id: str) -> bool:
    runtime = get_runtime()
    if sender_id == runtime.config.owner_qq:
        return True
    secondary_owner_qq = await runtime.store.get_secondary_owner_qq()
    return sender_id == secondary_owner_qq


def _build_group_reply_message(task: ApprovalTask) -> str | Message:
    if task.task_kind == ApprovalTaskKind.CLAIM:
        return task.reply_text
    if not task.reply_to_message_id or not task.target_user_id:
        raise ValueError("swap_match task missing reply target metadata")
    return (
        MessageSegment.reply(int(task.reply_to_message_id))
        + MessageSegment.at(task.target_user_id)
        + " 1"
    )


def _format_task_time(task: ApprovalTask) -> str:
    if task.task_kind == ApprovalTaskKind.SWAP_MATCH:
        return format_slot(task.matched_want_slot)
    if task.start_time and task.end_time:
        return f"{task.start_time}-{task.end_time}"
    return task.start_time or "未知"


async def _build_status_message() -> str:
    runtime = get_runtime()
    pending_tasks = await runtime.store.list_pending_tasks()
    swap_rules = await runtime.store.list_swap_watch_rules()
    secondary_owner_qq = await runtime.store.get_secondary_owner_qq()
    claim_mode = await runtime.store.get_claim_mode()
    claim_listening_paused = await runtime.store.is_claim_listening_paused()
    cooldown_remaining = await runtime.cooldown.get_remaining()
    lines = [
        "【当前状态】",
        f"第一主人：{runtime.config.owner_qq}",
        f"第二主人：{secondary_owner_qq or '未设置'}",
        f"送场监听：{'已暂停' if claim_listening_paused else '运行中'}",
        f"送场模式：{'自动' if claim_mode == ClaimMode.AUTO else '手动'}",
        (
            f"自动冷却：剩余 {_format_remaining_text(cooldown_remaining.total_seconds())}"
            if cooldown_remaining.total_seconds() > 0
            else "自动冷却：已就绪"
        ),
        f"换场规则：{len(swap_rules)} 条",
    ]
    if not pending_tasks:
        lines.append("待确认任务：无")
        lines.append("可用命令：/help")
        return "\n".join(lines)

    lines.append("待确认任务：")
    for task in pending_tasks:
        lines.append(
            f"- {task.token} | {task.task_kind.value} | {task.group_name} | {_format_task_time(task)} | 过期 {task.expires_at.strftime('%H:%M:%S')}"
        )
    lines.append("快捷操作：回复 1 确认，回复 0 取消")
    lines.append("可用命令：/pending /mode /secondary /swap")
    return "\n".join(lines)


async def _build_health_message() -> str:
    runtime = get_runtime()
    secondary_owner_qq = await runtime.store.get_secondary_owner_qq()
    swap_rules = await runtime.store.list_swap_watch_rules()
    claim_mode = await runtime.store.get_claim_mode()
    claim_listening_paused = await runtime.store.is_claim_listening_paused()
    cooldown_remaining = await runtime.cooldown.get_remaining()
    minimax_key = runtime.config.minimax.api_key or ""
    minimax_key_mask = (
        f"{minimax_key[:8]}...{minimax_key[-6:]}"
        if len(minimax_key) >= 16
        else ("已配置" if minimax_key else "未配置")
    )
    return "\n".join(
        [
            "【健康检查】",
            f"第一主人：{runtime.config.owner_qq}",
            f"第二主人：{secondary_owner_qq or '未设置'}",
            f"送场监听：{'已暂停' if claim_listening_paused else '运行中'}",
            f"送场模式：{'自动' if claim_mode == ClaimMode.AUTO else '手动'}",
            (
                f"自动冷却：剩余 {_format_remaining_text(cooldown_remaining.total_seconds())}"
                if cooldown_remaining.total_seconds() > 0
                else "自动冷却：已就绪"
            ),
            f"目标群数量：{len(runtime.config.target_groups)}",
            f"NapCat WS：{runtime.config.onebot.ws_url}",
            f"Access Token：{'已配置' if runtime.config.onebot.access_token else '未配置'}",
            f"MiniMax Endpoint：{runtime.config.minimax.endpoint}",
            f"MiniMax Model：{runtime.config.minimax.model}",
            f"MiniMax API Key：{minimax_key_mask}",
            f"状态文件：{runtime.config.storage_path}",
            f"换场规则：{len(swap_rules)} 条",
            "命令帮助：/help",
        ]
    )


async def _build_pending_message() -> str:
    runtime = get_runtime()
    pending_tasks = await runtime.store.list_pending_tasks()
    if not pending_tasks:
        return "【待确认任务】当前没有待确认任务"

    lines = ["【待确认任务】"]
    for task in pending_tasks:
        if task.task_kind == ApprovalTaskKind.SWAP_MATCH:
            lines.append(
                f"- {task.token} | swap_match | {task.group_name} | {task.sender_nickname} | 想换 {format_slot(task.matched_want_slot)} | 我可提供 {format_slot(task.matched_have_slot)} | 过期 {task.expires_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            lines.append(
                f"- {task.token} | claim | {task.group_name} | {task.sender_nickname} | {_format_task_time(task)} | 过期 {task.expires_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
    lines.append("只有 1 个待确认任务时，可直接回复 1 确认，回复 0 取消")
    return "\n".join(lines)


async def _build_cooldown_message() -> str:
    runtime = get_runtime()
    claim_mode = await runtime.store.get_claim_mode()
    remaining = await runtime.cooldown.get_remaining()
    lines = [
        "【自动模式冷却】",
        f"当前模式：{'自动' if claim_mode == ClaimMode.AUTO else '手动'}",
    ]
    if remaining.total_seconds() > 0:
        lines.append(f"剩余冷却：{_format_remaining_text(remaining.total_seconds())}")
    else:
        lines.append("剩余冷却：已就绪")
    lines.append("切换：/mode manual 或 /mode auto")
    lines.append("重置：/resetcooldown")
    return "\n".join(lines)


async def _build_mode_message() -> str:
    runtime = get_runtime()
    claim_mode = await runtime.store.get_claim_mode()
    claim_listening_paused = await runtime.store.is_claim_listening_paused()
    remaining = await runtime.cooldown.get_remaining()
    lines = [
        "【送场模式】",
        f"监听状态：{'已暂停' if claim_listening_paused else '运行中'}",
        f"当前模式：{'自动' if claim_mode == ClaimMode.AUTO else '手动'}",
    ]
    if claim_mode == ClaimMode.MANUAL:
        lines.append("手动模式：维持现状，需要主人私聊回复 1 / 0 确认")
    else:
        lines.append("自动模式：识别送场后等待 1 秒自动扣 1，并私聊主号结果")
    if remaining.total_seconds() > 0:
        lines.append(f"自动冷却：剩余 {_format_remaining_text(remaining.total_seconds())}")
    else:
        lines.append("自动冷却：已就绪")
    lines.append("切换：/mode manual 或 /mode auto")
    return "\n".join(lines)


async def _build_listen_message() -> str:
    runtime = get_runtime()
    paused = await runtime.store.is_claim_listening_paused()
    lines = [
        "【送场监听】",
        f"当前状态：{'已暂停' if paused else '运行中'}",
    ]
    if paused:
        lines.append("暂停期间：目标群送场消息不会生成待确认任务，也不会自动扣 1")
    else:
        lines.append("运行中：按当前 /mode 处理目标群送场消息")
    lines.append("切换：/listen pause 或 /listen resume")
    return "\n".join(lines)


async def _build_secondary_message() -> str:
    runtime = get_runtime()
    secondary_owner_qq = await runtime.store.get_secondary_owner_qq()
    return "\n".join(
        [
            "【第二主人】",
            f"第一主人：{runtime.config.owner_qq}",
            f"第二主人：{secondary_owner_qq or '未设置'}",
            "设置：/setsecondary <QQ号>",
            "移除：/removesecondary",
        ]
    )


async def _build_swapwatch_list_message() -> str:
    runtime = get_runtime()
    rules = await runtime.store.list_swap_watch_rules()
    if not rules:
        return "【换场监控】当前没有规则"

    lines = ["【换场监控】"]
    for rule in rules:
        lines.append(f"- {rule.name} ({rule.rule_id})")
        lines.append(f"  have: {'; '.join(format_slot(slot) for slot in rule.have_slots)}")
        lines.append(f"  want: {'; '.join(format_slot(slot) for slot in rule.want_slots)}")
    lines.append("添加：/swapwatch add <名称> | have: <槽位1>, <槽位2> | want: <槽位1>, <槽位2>")
    lines.append("删除：/swapwatch remove <rule_id>")
    lines.append("清空：/swapwatch clear")
    return "\n".join(lines)


def _build_swapwatch_help_message() -> str:
    return "\n".join(
        [
            "【换场监控命令】",
            "/swap 我有今晚78，周六12-13，我需要明晚89，周六11-12",
            "/swapwatch list: 查看全部换场监控规则",
            "/swapwatch add <名称> | have: <槽位1>, <槽位2> | want: <槽位1>, <槽位2>",
            "/swapwatch remove <rule_id>: 删除指定规则",
            "/swapwatch clear: 清空全部规则",
            "示例：/swapwatch add 后天连打 | have: 明天 4-5, 后天 4-5 | want: 后天 3-4, 后天 5-6",
        ]
    )


def _build_swap_simple_help_message() -> str:
    return "\n".join(
        [
            "【简化换场命令】",
            "/swap list: 查看全部换场监控规则",
            "/swap 我有今晚78，周六12-13，我需要明晚89，周六11-12",
        ]
    )


def _build_selflearn_help_message() -> str:
    return "\n".join(
        [
            "【自学习命令】",
            "/selflearn preview: 只读学习群并预览候选样本",
            "/selflearn run: 只向测试群发送样本并生成验证报告",
            "/selflearn apply <id>: 应用已确认的自学习规则",
            "安全限制：学习群只读，禁止作为测试发送目标",
        ]
    )


def _build_help_message() -> str:
    return "\n".join(
        [
            "【命令帮助】",
            "/help: 查看命令帮助",
            "/health: 查看连接与配置状态",
            "/status: 查看待确认任务概况",
            "/pending: 查看全部待确认任务",
            "/mode [manual|auto]: 切换或查看送场监听模式",
            "/listen [pause|resume]: 暂停或恢复监听送场消息",
            "也可直接发：暂停监听 / 恢复监听",
            "/cooldown: 查看自动模式冷却状态",
            "1 或 /1: 仅当只有 1 个待确认任务时，直接确认代发 1",
            "0 或 /0: 有待确认任务时取消当前候选；自动模式刚扣 1 后则撤回刚刚那条 1",
            "/resetcooldown: 重置自动模式冷却",
            "/resetall: 清空待确认任务",
            "/secondary: 查看第二主人",
            "/swap list: 查看全部换场监控规则",
            "/swap 我有今晚78，周六12-13，我需要明晚89，周六11-12",
            "/avatar: 立即从头像池随机更换 bot 头像",
            "/selflearn preview|run|apply: 自学习规则预览、验证和确认应用",
            "/restart: 重启 bot 进程",
            "/setsecondary <QQ>: 设置第二主人，仅第一主人可用",
            "/removesecondary: 移除第二主人，仅第一主人可用",
            "兼容旧命令：health / 状态",
        ]
    )


async def _build_minimax_probe_message() -> str:
    runtime = get_runtime()
    minimax_key = runtime.config.minimax.api_key or ""
    key_mask = (
        f"{minimax_key[:8]}...{minimax_key[-6:]}"
        if len(minimax_key) >= 16
        else ("已配置" if minimax_key else "未配置")
    )
    if not minimax_key:
        return "\n".join(
            [
                "【MiniMax 探测】",
                f"Endpoint：{runtime.config.minimax.endpoint}",
                f"Model：{runtime.config.minimax.model}",
                "API Key：未配置",
            ]
        )

    provider = runtime.parser.minimax
    if provider is None:
        return "\n".join(
            [
                "【MiniMax 探测】",
                f"Endpoint：{runtime.config.minimax.endpoint}",
                f"Model：{runtime.config.minimax.model}",
                f"API Key：{key_mask}",
                "Provider：未初始化",
            ]
        )

    try:
        result = await provider.debug_probe()
    except Exception as exc:
        return "\n".join(
            [
                "【MiniMax 探测】",
                f"Endpoint：{runtime.config.minimax.endpoint}",
                f"Model：{runtime.config.minimax.model}",
                f"API Key：{key_mask}",
                f"请求异常：{exc}",
            ]
        )

    body = result.get("body")
    body_text = str(body)
    if len(body_text) > 500:
        body_text = body_text[:500] + "..."
    return "\n".join(
        [
            "【MiniMax 探测】",
            f"Endpoint：{result.get('endpoint')}",
            f"Model：{result.get('model')}",
            f"API Key：{key_mask}",
            f"HTTP：{result.get('http_status')}",
            f"响应：{body_text}",
        ]
    )
