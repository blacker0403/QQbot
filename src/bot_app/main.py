from __future__ import annotations

import nonebot
from nonebot import get_driver, load_plugin
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from bot_app.config import load_app_config
from bot_app.logging_utils import configure_logging
from bot_app.runtime import build_runtime, set_runtime


def bootstrap() -> None:
    config = load_app_config()
    configure_logging(config.log_path)
    nonebot.init(
        driver="~fastapi+~websockets",
        onebot_ws_urls={config.onebot.ws_url},
        onebot_access_token=config.onebot.access_token,
    )
    driver = get_driver()
    driver.register_adapter(OneBotV11Adapter)
    set_runtime(build_runtime(config))
    load_plugin("bot_app.plugins.message_ingest")
    load_plugin("bot_app.plugins.private_commands")
    load_plugin("bot_app.plugins.avatar_rotation")
