"""A token's last day on the chain, from the trades the watcher keeps: counts and
volume of buys and sells, market cap now, high and low, change, biggest buy.
Pure: trade points in, numbers out.
"""

from dataclasses import dataclass


@dataclass
class TokenDay:
    hours: float        # how much time the trades cover, at most the window asked for
    buys: int
    sells: int
    buy_volume: float   # quote units
    sell_volume: float
    mc_now: float       # market cap in quote units
    mc_high: float
    mc_low: float
    change: float | None  # fraction over the window: 0.25 is +25%
    biggest_buy: float    # quote units


def token_day(points: list[tuple[int, float, float, int]], supply: float, now: float,
              window: float = 86400) -> TokenDay | None:
    """points are (time, price after the trade, size in quote, side 1/-1), any order."""
    points = sorted(p for p in points if now - window <= p[0] <= now and p[1] > 0)
    if not points:
        return None
    buys = [p for p in points if p[3] > 0]
    sells = [p for p in points if p[3] < 0]
    caps = [p[1] * supply for p in points]
    first, last = caps[0], caps[-1]
    return TokenDay(
        hours=max(0.0, (now - points[0][0]) / 3600) if now - points[0][0] < window * 0.95 else window / 3600,
        buys=len(buys), sells=len(sells),
        buy_volume=sum(p[2] for p in buys), sell_volume=sum(p[2] for p in sells),
        mc_now=last, mc_high=max(caps), mc_low=min(caps),
        change=(last / first - 1) if first > 0 and len(points) > 1 else None,
        biggest_buy=max((p[2] for p in buys), default=0.0),
    )
