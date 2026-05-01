from __future__ import annotations

from datetime import datetime, time
import json
import re
from typing import Any

import httpx

from bot_app.config import MiniMaxConfig
from bot_app.models import ExchangeParseResult, LLMDecision, ResolvedSlot, SwapWatchRule


class MiniMaxProvider:
    def __init__(self, config: MiniMaxConfig) -> None:
        self.config = config

    async def assess_offer(
        self,
        text: str,
        target_aliases: list[str],
        min_time: time,
        local_rule_context: dict[str, Any] | None = None,
    ) -> LLMDecision:
        context_lines: list[str] = []
        if local_rule_context:
            context_lines.extend(
                [
                    "本地规则初判上下文：",
                    json.dumps(local_rule_context, ensure_ascii=False, default=str),
                    "复核要求：本地规则可能已经自动扣 1。你必须判断原消息是否真的是发送者本人送出/转让场地；询问、求场、问有没有人送、反问句都必须返回 is_real_offer=false。",
                ]
            )
        prompt = "\n".join(
            [
                "你是羽毛球场地转让消息分类器。",
                "只判断这条 QQ 群消息是否是在送出或转让羽毛球场地。",
                f"目标校区别名：{', '.join(target_aliases)}",
                f"仅接受开始时间不早于 {min_time.strftime('%H:%M')} 的场地。",
                "规则：如果消息没有写校区，默认按目标校区处理。",
                "规则：45/56/67/78/89 这类短写默认表示下午或晚上时间段，45=16:00-17:00，67=18:00-19:00，78=19:00-20:00，89=20:00-21:00。",
                "规则：七八/八九，以及 7八/6七 这类中阿混写，也按同样的晚间时间段理解。",
                "规则：9-10 和 9-11 默认是早上 09:00-10:00 / 09:00-11:00；8-9 默认是晚上 20:00-21:00。",
                "如果无法确定，必须保守返回 is_real_offer=false。",
                "不要输出 <think>、推理过程或解释段落。",
                *context_lines,
                '只输出 JSON，不要输出额外文本。格式如下：{"is_real_offer":true,"campus":"湖区","start_time":"18:00","confidence":0.91,"reason":"..."}',
                f"消息：{text}",
            ]
        )
        response = await self._post_prompt(prompt, max_tokens=600)
        content = self._extract_content(response)
        decision_data = self._extract_json(content)
        return LLMDecision.model_validate(decision_data)

    async def assess_exchange(
        self,
        text: str,
        reference_time: datetime,
        target_aliases: list[str],
    ) -> ExchangeParseResult:
        prompt = "\n".join(
            [
                "你是羽毛球场地换场消息解析器。",
                "请从 QQ 群消息中提取对方当前手里有的场地，以及对方想换到的场地。",
                "如果信息不完整或无法确定，必须保守返回空列表。",
                f"当前时间：{reference_time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"目标校区别名：{', '.join(target_aliases)}",
                "规则：如果消息没有写校区，默认按目标校区处理。",
                "规则：45/56/67/78/89 这类短写默认表示时间段，45=16:00-17:00，67=18:00-19:00，89=20:00-21:00。",
                "规则：9-10 和 9-11 默认是早上 09:00-10:00 / 09:00-11:00；8-9 默认是晚上 20:00-21:00。",
                "规则：同一条换场消息里，后半句如果省略日期或校区，优先继承前半句的日期和校区。例如“今天67换45”表示“今天67换今天45”。",
                (
                    '只输出 JSON，不要输出额外文本。格式如下：'
                    '{"their_have_slots":[{"date":"2026-04-24","start_time":"16:00","end_time":"17:00","campus":"湖区","raw_text":"明天4-5"}],'
                    '"their_want_slots":[{"date":"2026-04-25","start_time":"15:00","end_time":"16:00","campus":"湖区","raw_text":"后天3-4"}],'
                    '"confidence":0.9,"reason":"..."}'
                ),
                f"消息：{text}",
            ]
        )
        response = await self._post_prompt(prompt, max_tokens=1000)
        content = self._extract_content(response)
        decision_data = self._extract_json(content)
        their_have_slots = [
            ResolvedSlot.model_validate(slot)
            for slot in decision_data.get("their_have_slots", [])
            if isinstance(slot, dict)
        ]
        their_want_slots = [
            ResolvedSlot.model_validate(slot)
            for slot in decision_data.get("their_want_slots", [])
            if isinstance(slot, dict)
        ]
        return ExchangeParseResult(
            is_exchange_candidate=bool(their_have_slots and their_want_slots),
            their_have_slots=their_have_slots,
            their_want_slots=their_want_slots,
            confidence=float(decision_data.get("confidence", 0.0)),
            reason=str(decision_data.get("reason", "")),
            needs_llm=False,
        )

    async def assess_swap_match(
        self,
        rule: SwapWatchRule,
        their_have_slots: list[ResolvedSlot],
        their_want_slots: list[ResolvedSlot],
    ) -> tuple[int | None, int | None]:
        prompt = "\n".join(
            [
                "You are matching badminton court swap rules.",
                "Decide whether the other person's swap message matches my rule.",
                "My have slots are the courts I can give.",
                "My want slots are the courts I want to get. A want slot may be exact or 'start_after'.",
                "Their want slots are the courts they are asking from me.",
                "Their have slots are the courts they can give me.",
                "A valid match requires both sides to match at the same time.",
                (
                    'Return JSON only like '
                    '{"matched_have_index":0,"matched_want_index":1,"reason":"..."} '
                    "Use -1 when there is no valid match."
                ),
                f"my_have_slots={json.dumps([slot.model_dump(mode='json') for slot in rule.have_slots], ensure_ascii=False)}",
                f"my_want_slots={json.dumps([slot.model_dump(mode='json') for slot in rule.want_slots], ensure_ascii=False)}",
                f"their_have_slots={json.dumps([slot.model_dump(mode='json') for slot in their_have_slots], ensure_ascii=False)}",
                f"their_want_slots={json.dumps([slot.model_dump(mode='json') for slot in their_want_slots], ensure_ascii=False)}",
            ]
        )
        response = await self._post_prompt(prompt, max_tokens=600)
        content = self._extract_content(response)
        decision_data = self._extract_json(content)
        matched_have_index = int(decision_data.get("matched_have_index", -1))
        matched_want_index = int(decision_data.get("matched_want_index", -1))
        if matched_have_index < 0 or matched_want_index < 0:
            return None, None
        return matched_have_index, matched_want_index

    async def parse_swap_command(
        self,
        *,
        text: str,
        reference_time: datetime,
        target_aliases: list[str],
    ) -> list[tuple[list[ResolvedSlot], list[ResolvedSlot]]]:
        prompt = "\n".join(
            [
                "You parse my /swap command for badminton court swap monitoring.",
                "Extract the courts I have and the courts I want. The user may write one or multiple swap groups.",
                "Return an empty groups array when either side is missing or ambiguous.",
                "本地换场规则前置信息：",
                "1. /swap 后面的自然语言是在描述我自己的换场需求，不是群里别人的换场消息。",
                "2. 如果没有写“我有/我想要”，但句子包含“换”，换字前就是我有的场地，换字后就是我想换到的场地。",
                "3. “或者/或/、/，”连接在换字后时，表示同一个 have 可以接受多个 want，必须全部放进 want_slots。",
                "4. 例：周一12-13，换13-14或者11-12 => have_slots=[周一12-13]，want_slots=[周一13-14, 周一11-12]。",
                "5. 换字后的场地如果省略日期或校区，继承换字前场地的日期和校区。",
                "6. 如果 want 侧有多个选项，后一个选项省略日期时，优先继承 want 侧前一个选项的日期；没有前一个 want 日期时再继承 have 日期。",
                "7. 日期按当前时间解析；周一/周二等指未来最近的对应星期。",
                "送场地规则前置信息：送/出/转/场地/一片通常表示转让场地；有人送吗、有人送不、有没有人送、求场、收场不是送场。",
                "换场地规则前置信息：想换/要换/换到/换 表示交换场地；一条换场需求必须同时有 have_slots 和 want_slots。",
                f"Current time: {reference_time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"Target campus aliases: {', '.join(target_aliases)}",
                "If campus is omitted, use the first target campus alias.",
                "Use HH:MM 24-hour time. Understand short ranges like 56/67/78/89 as evening ranges.",
                (
                    'Return JSON only like {"groups":[{"have_slots":[{"date":"2026-04-26",'
                    '"start_time":"19:00","end_time":"20:00","campus":"湖区","raw_text":"明晚78"}],'
                    '"want_slots":[{"date":"2026-04-27","start_time":"20:00","end_time":"21:00",'
                    '"campus":"湖区","raw_text":"后天89"}]}]}'
                ),
                f"Command text: {text}",
            ]
        )
        decision_data = await self._post_json_with_retry(
            prompt,
            max_tokens=4096,
            retry_prompt="\n".join(
                [
                    prompt,
                    "",
                    "上一次返回只有思考内容或被长度截断。",
                    "现在禁止输出任何 <think>、分析、解释、Markdown。",
                    "只输出最终 JSON 对象，第一字符必须是 {，最后字符必须是 }。",
                ]
            ),
        )
        group_items = decision_data.get("groups")
        if not isinstance(group_items, list):
            group_items = [decision_data]

        groups: list[tuple[list[ResolvedSlot], list[ResolvedSlot]]] = []
        for item in group_items:
            if not isinstance(item, dict):
                continue
            have_slots = [
                ResolvedSlot.model_validate(slot)
                for slot in item.get("have_slots", [])
                if isinstance(slot, dict)
            ]
            want_slots = [
                ResolvedSlot.model_validate(slot)
                for slot in item.get("want_slots", [])
                if isinstance(slot, dict)
            ]
            if have_slots and want_slots:
                groups.append((have_slots, want_slots))
        return groups

    async def _post_json_with_retry(self, prompt: str, max_tokens: int, retry_prompt: str | None = None) -> dict:
        response = await self._post_prompt(prompt, max_tokens=max_tokens)
        try:
            return self._extract_json(self._extract_content(response))
        except ValueError:
            if retry_prompt is None:
                raise
        retry_response = await self._post_prompt(retry_prompt, max_tokens=max_tokens)
        return self._extract_json(self._extract_content(retry_response))

    async def assess_selflearn_consistency(
        self,
        *,
        text: str,
        expected_action: str,
        actual_action: str,
        response_summary: str,
    ) -> bool:
        prompt = "\n".join(
            [
                "你是 QQ 羽毛球场地 bot 测试结果判定器。",
                "判断 bot 的实际响应是否符合预期业务动作。",
                "规则：offer 表示送场，应触发候选场地或自动扣 1；exchange 表示换场，应触发换场匹配通知且不应自动扣 1；none 表示不应响应。",
                '只输出 JSON，不要输出额外文本。格式如下：{"consistent":true,"reason":"..."}',
                f"消息：{text}",
                f"预期动作：{expected_action}",
                f"实际动作：{actual_action}",
                f"响应摘要：{response_summary}",
            ]
        )
        response = await self._post_prompt(prompt, max_tokens=400)
        content = self._extract_content(response)
        decision_data = self._extract_json(content)
        return bool(decision_data.get("consistent", False))

    async def _post_prompt(self, prompt: str, max_tokens: int) -> dict:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "reasoning_split": False,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {
                    "role": "system",
                    "name": "classifier",
                    "content": "You output compact JSON only. Do not output <think> or reasoning text.",
                },
                {"role": "user", "name": "user", "content": prompt},
            ],
        }
        timeout = httpx.Timeout(connect=5.0, read=self.config.timeout_seconds, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self.config.endpoint, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    async def debug_probe(self) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "max_tokens": 50,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "name": "tester", "content": "Reply with OK."},
                {"role": "user", "name": "user", "content": "OK?"},
            ],
        }
        timeout = httpx.Timeout(connect=5.0, read=self.config.timeout_seconds, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self.config.endpoint, headers=headers, json=payload)
            result: dict[str, Any] = {
                "http_status": response.status_code,
                "endpoint": self.config.endpoint,
                "model": self.config.model,
            }
            try:
                result["body"] = response.json()
            except Exception:
                result["body"] = response.text
            return result

    @staticmethod
    def _extract_content(response: dict[str, Any]) -> str:
        base_resp = response.get("base_resp")
        if isinstance(base_resp, dict):
            status_code = base_resp.get("status_code")
            status_msg = base_resp.get("status_msg")
            if status_code not in (None, 0):
                raise ValueError(f"MiniMax base_resp error: code={status_code}, msg={status_msg}")

        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError(
                "MiniMax response missing choices. "
                f"keys={list(response.keys())}, response={json.dumps(response, ensure_ascii=False)}"
            )

        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ValueError(
                "MiniMax response missing message object. "
                f"response={json.dumps(response, ensure_ascii=False)}"
            )

        content = message.get("content")
        if isinstance(content, str):
            content = MiniMaxProvider._strip_thinking(content)
            if not content.strip():
                raise ValueError(
                    "MiniMax returned empty content. "
                    f"response={json.dumps(response, ensure_ascii=False)}"
                )
            return content

        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_value = item.get("text")
                    if isinstance(text_value, str):
                        text_parts.append(MiniMaxProvider._strip_thinking(text_value))
            if text_parts:
                return "\n".join(text_parts)

        raise ValueError(
            "MiniMax response missing usable content. "
            f"response={json.dumps(response, ensure_ascii=False)}"
        )

    @staticmethod
    def _strip_thinking(content: str) -> str:
        return re.sub(r"<think>.*?</think>", "", content, flags=re.S | re.I).strip()

    @staticmethod
    def _extract_json(content: str) -> dict:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", content):
            try:
                decoded, _ = decoder.raw_decode(content[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
        fallback = MiniMaxProvider._extract_partial_json(content)
        if fallback is not None:
            return fallback
        raise ValueError(f"MiniMax returned no JSON payload: {content}")

    @staticmethod
    def _extract_partial_json(content: str) -> dict[str, Any] | None:
        if "{" not in content:
            return None
        candidate = content[content.rfind("{"):]
        data: dict[str, Any] = {}

        if match := re.search(r'"is_real_offer"\s*:\s*(true|false)', candidate):
            data["is_real_offer"] = match.group(1) == "true"
        if match := re.search(r'"campus"\s*:\s*"([^"]*)"', candidate):
            data["campus"] = match.group(1)
        if match := re.search(r'"start_time"\s*:\s*"([^"]*)"', candidate):
            data["start_time"] = match.group(1)
        if match := re.search(r'"confidence"\s*:\s*(-?\d+(?:\.\d+)?)', candidate):
            data["confidence"] = float(match.group(1))
        if match := re.search(r'"matched_have_index"\s*:\s*(-?\d+)', candidate):
            data["matched_have_index"] = int(match.group(1))
        if match := re.search(r'"matched_want_index"\s*:\s*(-?\d+)', candidate):
            data["matched_want_index"] = int(match.group(1))
        if match := re.search(r'"reason"\s*:\s*"([^"]*)', candidate, flags=re.S):
            data["reason"] = match.group(1).strip()

        if not data:
            return None
        data.setdefault("reason", "MiniMax 返回内容被截断，已按可提取字段降级解析")
        data.setdefault("their_have_slots", [])
        data.setdefault("their_want_slots", [])
        return data
