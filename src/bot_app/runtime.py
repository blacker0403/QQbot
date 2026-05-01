from __future__ import annotations

from dataclasses import dataclass

from bot_app.config import AppConfig
from bot_app.services.approval import ApprovalService
from bot_app.services.cooldown import CooldownService
from bot_app.services.exchange_parse import ExchangeParseService
from bot_app.services.minimax import MiniMaxProvider
from bot_app.services.notify import NotifyService
from bot_app.services.prefilter import PrefilterService
from bot_app.services.self_learning import SelfLearningService
from bot_app.services.semantic_parse import SemanticParseService
from bot_app.services.slot_parser import SlotParser
from bot_app.services.workflow import ClaimWorkflow
from bot_app.storage import JsonStateStore


@dataclass(slots=True)
class AppRuntime:
    config: AppConfig
    store: JsonStateStore
    prefilter: PrefilterService
    slot_parser: SlotParser
    parser: SemanticParseService
    exchange_parser: ExchangeParseService
    approval: ApprovalService
    cooldown: CooldownService
    notifier: NotifyService
    workflow: ClaimWorkflow
    self_learning: SelfLearningService


_RUNTIME: AppRuntime | None = None


def set_runtime(runtime: AppRuntime) -> None:
    global _RUNTIME
    _RUNTIME = runtime


def get_runtime() -> AppRuntime:
    if _RUNTIME is None:
        raise RuntimeError("App runtime has not been initialized.")
    return _RUNTIME


def build_runtime(config: AppConfig) -> AppRuntime:
    store = JsonStateStore(config.storage_path)
    if config.secondary_owner_qq and not store._state.secondary_owner_qq:
        store._state.secondary_owner_qq = config.secondary_owner_qq
        store._write_to_disk()
    prefilter = PrefilterService(config.keywords.include, config.keywords.exclude)
    slot_parser = SlotParser(config)
    minimax = MiniMaxProvider(config.minimax) if config.minimax.api_key else None
    parser = SemanticParseService(config, minimax)
    exchange_parser = ExchangeParseService(slot_parser, minimax)
    approval = ApprovalService(config, store)
    cooldown = CooldownService(config, store)
    notifier = NotifyService(config, store)
    workflow = ClaimWorkflow(
        config=config,
        store=store,
        prefilter=prefilter,
        slot_parser=slot_parser,
        exchange_parser=exchange_parser,
        parser=parser,
        approval=approval,
        cooldown=cooldown,
        notifier=notifier,
    )
    self_learning = SelfLearningService(config, workflow)
    runtime = AppRuntime(
        config=config,
        store=store,
        prefilter=prefilter,
        slot_parser=slot_parser,
        parser=parser,
        exchange_parser=exchange_parser,
        approval=approval,
        cooldown=cooldown,
        notifier=notifier,
        workflow=workflow,
        self_learning=self_learning,
    )
    return runtime
