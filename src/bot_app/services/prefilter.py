from __future__ import annotations


class PrefilterService:
    def __init__(self, include_keywords: list[str], exclude_keywords: list[str]) -> None:
        self.include_keywords = include_keywords
        self.exclude_keywords = exclude_keywords

    def match(self, text: str) -> bool:
        compact = text.strip()
        if not compact:
            return False
        if any(keyword in compact for keyword in self.exclude_keywords):
            return False
        return any(keyword in compact for keyword in self.include_keywords)

