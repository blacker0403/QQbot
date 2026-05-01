from __future__ import annotations

from datetime import time
from pathlib import Path
import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field
import yaml


class KeywordsConfig(BaseModel):
    include: list[str] = Field(default_factory=lambda: ["送", "出", "转", "场地", "一片"])
    exclude: list[str] = Field(default_factory=list)


class OneBotConfig(BaseModel):
    ws_url: str = "ws://127.0.0.1:3001/onebot/v11/ws"
    access_token: str | None = None


class MiniMaxConfig(BaseModel):
    endpoint: str = "https://api.minimaxi.com/v1/chat/completions"
    model: str = "MiniMax-M2.7"
    api_key: str | None = None
    timeout_seconds: float = 60.0


class SelfLearningConfig(BaseModel):
    source_group_id: str = ""
    test_group_id: str = ""
    candidates_path: str = "data/selflearn_candidates.json"
    learned_rules_path: str = "data/learned_rules.json"
    history_search_roots: list[str] = Field(default_factory=list)
    history_auto_search_enabled: bool = True
    history_max_files: int = 200
    history_max_file_bytes: int = 2_000_000


class AvatarRotationConfig(BaseModel):
    enabled: bool = False
    interval_hours: float = Field(default=24.0, gt=0)
    random_jitter_minutes: float = Field(default=120.0, ge=0)
    initial_delay_seconds: float = Field(default=300.0, ge=0)
    image_directory: str = "data/avatar_pool"
    image_urls: list[str] = Field(default_factory=list)
    nickname_pool: list[str] = Field(
        default_factory=lambda: [
            "Limerence",
            "Petrichor",
            "Eunoia",
            "Sonder",
            "Vellichor",
            "Ephemera",
            "Aestus",
            "Solivagant",
            "Susurrus",
            "Nyctophile",
            "Halcyon",
            "Ineffable",
            "Ethereal",
            "Reverie",
            "Serendipity",
            "Mellifluous",
            "Umbra",
            "Luminous",
            "Nocturne",
            "Serein",
        ]
    )


class AppConfig(BaseModel):
    owner_qq: str
    secondary_owner_qq: str | None = None
    target_groups: list[str] = Field(default_factory=list)
    target_campus_key: str = "huqu"
    campus_aliases: dict[str, list[str]] = Field(default_factory=dict)
    min_start_time: str = "17:00"
    cooldown_hours: int = 48
    approval_ttl_seconds: int = 900
    storage_path: str = "data/state.json"
    log_path: str = "logs/bot.log"
    keywords: KeywordsConfig = Field(default_factory=KeywordsConfig)
    onebot: OneBotConfig = Field(default_factory=OneBotConfig)
    minimax: MiniMaxConfig = Field(default_factory=MiniMaxConfig)
    self_learning: SelfLearningConfig = Field(default_factory=SelfLearningConfig)
    avatar_rotation: AvatarRotationConfig = Field(default_factory=AvatarRotationConfig)

    @property
    def min_start_time_obj(self) -> time:
        hour_text, minute_text = self.min_start_time.split(":")
        return time(hour=int(hour_text), minute=int(minute_text))

    @property
    def target_campus_aliases(self) -> list[str]:
        return self.campus_aliases.get(self.target_campus_key, [])


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _apply_env_overrides(data: dict) -> dict:
    data.setdefault("onebot", {})
    data.setdefault("minimax", {})
    data.setdefault("self_learning", {})
    data.setdefault("avatar_rotation", {})
    if ws_url := os.getenv("NAPCAT_WS_URL"):
        data["onebot"]["ws_url"] = ws_url
    if access_token := os.getenv("NAPCAT_ACCESS_TOKEN"):
        data["onebot"]["access_token"] = access_token
    if api_key := os.getenv("MINIMAX_API_KEY"):
        data["minimax"]["api_key"] = api_key
    return data


def load_app_config() -> AppConfig:
    load_dotenv(dotenv_path=Path(".env"), override=True)
    config_path = Path(os.getenv("BOT_CONFIG_PATH", "config.yaml"))
    raw_data = _load_yaml(config_path)
    merged = _apply_env_overrides(raw_data)
    return AppConfig.model_validate(merged)
