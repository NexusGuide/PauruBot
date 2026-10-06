"""Top-up bonus per payment method (PAY-14).

A rule is {"provider": "card" | "*" | ..., "min_amount": int, "percent": 1-100, "max_bonus": int | null}.
The most generous matching rule wins; the bonus is a separate reward journal, so it shows up as
its own line in the wallet history and never mixes with the paid amount.
"""

from __future__ import annotations

from typing import Any


class BonusRuleError(ValueError):
    pass


def validate_rules(value: Any, providers: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 20:
        raise BonusRuleError("bonus_rules_invalid")
    clean: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise BonusRuleError("bonus_rules_invalid")
        provider = str(item.get("provider") or "*")
        if provider != "*" and provider not in providers:
            raise BonusRuleError("bonus_rules_invalid")
        try:
            percent = int(item.get("percent") or 0)
            minimum = int(item.get("min_amount") or 0)
            cap = item.get("max_bonus")
            cap = int(cap) if cap not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise BonusRuleError("bonus_rules_invalid") from exc
        if not 1 <= percent <= 100 or minimum < 0 or (cap is not None and cap <= 0):
            raise BonusRuleError("bonus_rules_invalid")
        clean.append({"provider": provider, "min_amount": minimum, "percent": percent, "max_bonus": cap})
    return clean


def bonus_for(rules: list[dict[str, Any]] | None, provider: str, amount: int) -> int:
    best = 0
    for rule in rules or []:
        if not isinstance(rule, dict) or rule.get("provider") not in ("*", provider):
            continue
        if amount < int(rule.get("min_amount") or 0):
            continue
        value = amount * int(rule.get("percent") or 0) // 100
        cap = rule.get("max_bonus")
        if cap:
            value = min(value, int(cap))
        best = max(best, value)
    return best
