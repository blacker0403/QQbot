from __future__ import annotations

from pathlib import Path
import json
from typing import Any


def load_rules(path: str) -> list[dict[str, Any]]:
    rules_path = Path(path)
    if not rules_path.exists():
        return []
    try:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rules = data.get("rules")
    if not isinstance(rules, list):
        return []
    return [rule for rule in rules if isinstance(rule, dict)]


def save_rules(path: str, rules: list[dict[str, Any]]) -> None:
    rules_path = Path(path)
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2), encoding="utf-8")


def match_rule(path: str, text: str, kinds: set[str]) -> dict[str, Any] | None:
    for rule in load_rules(path):
        kind = str(rule.get("kind", ""))
        if kind not in kinds:
            continue
        pattern = str(rule.get("pattern", ""))
        if not pattern:
            continue
        match_type = str(rule.get("match_type", "exact"))
        if match_type == "contains" and pattern in text:
            return rule
        if match_type == "exact" and pattern == text:
            return rule
    return None
