from __future__ import annotations

from dataclasses import dataclass
import logging
import random
from pathlib import Path
from typing import Protocol

from bot_app.config import AvatarRotationConfig

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class AvatarRotationResult:
    avatar: str
    nickname: str | None


class AvatarBot(Protocol):
    async def call_api(self, api: str, **kwargs):
        ...


class AvatarRotator:
    def __init__(self, config: AvatarRotationConfig, rng: random.Random | None = None) -> None:
        self.config = config
        self.rng = rng or random.Random()

    def collect_candidates(self) -> list[str]:
        candidates: list[str] = []
        directory = Path(self.config.image_directory)
        if directory.exists():
            candidates.extend(
                str(path)
                for path in sorted(directory.iterdir())
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            )
        candidates.extend(url.strip() for url in self.config.image_urls if url.strip())
        return candidates

    def choose_candidate(self) -> str | None:
        candidates = self.collect_candidates()
        if not candidates:
            return None
        return self.rng.choice(candidates)

    def choose_nickname(self) -> str | None:
        nicknames = [name.strip() for name in self.config.nickname_pool if name.strip()]
        if not nicknames:
            return None
        return self.rng.choice(nicknames)

    async def rotate_once(self, bot: AvatarBot) -> AvatarRotationResult | None:
        candidate = self.choose_candidate()
        if candidate is None:
            logger.warning("Avatar rotation skipped: no local images or image URLs configured")
            return None
        await bot.call_api("set_qq_avatar", file=candidate)
        logger.info("Avatar rotation updated QQ avatar from %s", candidate)
        nickname = self.choose_nickname()
        if nickname:
            await bot.call_api("set_qq_profile", nickname=nickname, sex=1)
            logger.info("Avatar rotation updated QQ nickname to %s", nickname)
        return AvatarRotationResult(avatar=candidate, nickname=nickname)
