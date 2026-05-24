from __future__ import annotations

import asyncio
from datetime import time
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
from bot_app.models import LLMDecision, ParsedCandidate
from bot_app.services.semantic_parse import SemanticParseService


class RejectingOfferLLM:
    async def assess_offer(self, **kwargs) -> LLMDecision:
        return LLMDecision(
            is_real_offer=False,
            campus="九龙湖",
            start_time=None,
            confidence=0.99,
            reason="not an offer",
        )


class SemanticParseTest(unittest.TestCase):
    def make_parser(self) -> SemanticParseService:
        config = AppConfig(
            owner_qq="10000",
            target_groups=["target-group"],
            target_campus_key="jiulonghu",
            campus_aliases={"jiulonghu": ["九龙湖"]},
            min_start_time="17:00",
        )
        return SemanticParseService(config, RejectingOfferLLM())

    def test_short_leading_offer_is_definitive_for_auto_claim_review(self) -> None:
        parser = self.make_parser()
        parsed = ParsedCandidate(
            is_candidate=True,
            campus="九龙湖",
            start_time=time(17, 0),
            end_time=time(18, 0),
            confidence=0.9,
        )

        verdict = asyncio.run(parser.verify_offer_with_llm("送5-6", parsed))

        self.assertIs(verdict, True)

    def test_question_like_offer_still_allows_llm_rejection(self) -> None:
        parser = self.make_parser()
        parsed = ParsedCandidate(
            is_candidate=True,
            campus="九龙湖",
            start_time=time(17, 0),
            end_time=time(18, 0),
            confidence=0.9,
        )

        verdict = asyncio.run(parser.verify_offer_with_llm("有人送晚上56不", parsed))

        self.assertIs(verdict, False)


if __name__ == "__main__":
    unittest.main()
