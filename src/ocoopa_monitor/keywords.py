from __future__ import annotations

from typing import Iterable, List

from .models import Keyword


DEFAULT_KEYWORDS: List[Keyword] = [
    Keyword("Ocoopa", "brand", "high"),
    Keyword("Ocopa", "brand", "high"),
    Keyword("Ocoopa hand warmer", "brand", "high"),
    Keyword("Ocoopa fire", "incident", "high"),
    Keyword("Ocoopa burn", "incident", "high"),
    Keyword("Ocoopa overheat", "incident", "high"),
    Keyword("Ocoopa explode", "incident", "high"),
    Keyword("Ocoopa death", "incident", "high"),
    Keyword("Ocoopa danger", "incident", "high"),
    Keyword("Ocoopa lawsuit", "legal", "high"),
    Keyword("Ocoopa wrongful death", "legal", "high"),
    Keyword("Ocoopa product liability", "legal", "high"),
    Keyword("Ocoopa class action", "legal", "high"),
    Keyword("Ocoopa settlement", "legal", "high"),
    Keyword("Ocoopa recall", "regulatory", "high"),
    Keyword("Ocoopa CPSC", "regulatory", "high"),
    Keyword("Amazon Ocoopa", "channel", "high"),
    Keyword("Ocoopa removed", "channel", "high"),
    Keyword("Ocoopa delisted", "channel", "high"),
    Keyword("Ocoopa HR-", "model", "high"),
    Keyword("Ocoopa power bank fire", "incident", "high"),
    Keyword("hand warmer fire", "category", "regular"),
    Keyword("hand warmer death", "category", "regular"),
    Keyword("hand warmer lawsuit", "category", "regular"),
    Keyword("rechargeable hand warmer recall", "category", "regular"),
    Keyword("electric hand warmer burn", "category", "regular"),
    Keyword("Ocoopa 诉讼", "legal", "high"),
    Keyword("Ocoopa 起火", "incident", "high"),
    Keyword("Ocoopa 召回", "regulatory", "high"),
    Keyword("Ocoopa 暖手宝 爆炸", "incident", "high"),
    Keyword("充电暖手宝 致死", "category", "regular"),
    Keyword("暖手宝 起火 死亡", "category", "regular"),
]


def terms_for_lane(keywords: Iterable[Keyword], lane: str) -> List[str]:
    if lane == "high":
        allowed = {"high"}
    else:
        allowed = {"high", "regular"}
    return [keyword.term for keyword in keywords if keyword.active and keyword.lane in allowed]
