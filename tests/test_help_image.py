from __future__ import annotations

from pathlib import Path
import sys
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
onebot_v11.PrivateMessageEvent = object


class _MessageSegment:
    def __init__(self, segment_type: str, data: dict) -> None:
        self.type = segment_type
        self.data = data

    @classmethod
    def image(cls, **kwargs):
        return cls("image", kwargs)


onebot_v11.MessageSegment = _MessageSegment
sys.modules["nonebot.adapters.onebot.v11"] = onebot_v11

import bot_app.plugins.private_commands as private_commands
from bot_app.plugins.private_commands import _help_image_path

private_commands.MessageSegment = _MessageSegment


class HelpImageTest(unittest.TestCase):
    def test_help_image_asset_exists_and_is_png(self) -> None:
        image_path = _help_image_path()

        self.assertTrue(image_path.exists())
        self.assertTrue(image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_help_image_message_uses_local_file_uri(self) -> None:
        message = private_commands._build_help_image_message()

        self.assertEqual(message.type, "image")
        self.assertTrue(message.data["file"].startswith("file:///"))
        self.assertTrue(message.data["file"].endswith("/help_card_imagegen_20260511.png"))


if __name__ == "__main__":
    unittest.main()
