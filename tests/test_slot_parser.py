from __future__ import annotations

from datetime import datetime
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

from bot_app.config import AppConfig
from bot_app.services.slot_parser import SlotParser


class SlotParserTest(unittest.TestCase):
    def setUp(self) -> None:
        config = AppConfig(
            owner_qq="10000",
            target_groups=["target-group"],
            target_campus_key="jiulonghu",
            campus_aliases={"jiulonghu": ["九龙湖"]},
        )
        self.parser = SlotParser(config)

    def test_short_month_day_before_time_range(self) -> None:
        slot = self.parser.parse_slot(
            "送5.1  晚上8-9",
            reference_time=datetime(2026, 4, 30, 21, 0, 15),
        )

        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertEqual(slot.date, "2026-05-01")
        self.assertEqual(slot.start_time, "20:00")
        self.assertEqual(slot.end_time, "21:00")

    def test_hyphen_time_range_is_not_treated_as_short_date(self) -> None:
        slot = self.parser.parse_slot(
            "送 晚上8-9",
            reference_time=datetime(2026, 4, 30, 21, 0, 15),
        )

        self.assertIsNone(slot)


if __name__ == "__main__":
    unittest.main()
