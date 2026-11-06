"""Тарифы и пересчёт токенов в деньги.

Пик: 01:00-04:00 и 06:00-10:00 UTC, пн-пт. Остальное — непик, в полцены.

Известное ограничение: непиковый тариф действует ещё и в государственные
праздники КНР, календаря которых у нас нет. В эти дни мы считаем по пиковому
и завышаем расход вдвое.
"""

from datetime import datetime, timezone

import config

PEAK_WINDOWS_UTC = ((1, 4), (6, 10))


def tariff_at(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    if moment.weekday() >= 5:
        return "offpeak"
    for start, end in PEAK_WINDOWS_UTC:
        if start <= moment.hour < end:
            return "peak"
    return "offpeak"


def cost_usd(cache_hit: int, cache_miss: int, output: int, tariff: str) -> float:
    p = config.PRICES[tariff]
    return (
        cache_hit * p["cache_hit"]
        + cache_miss * p["cache_miss"]
        + output * p["output"]
    ) / 1_000_000


def to_millicents(usd: float) -> float:
    """1 доллар = 100 центов = 100 000 милли-центов."""
    return usd * 100_000
