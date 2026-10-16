"""Read-only synthetic demonstration, never stored in the user's database."""
from datetime import date, timedelta
import random

from .domain import overview, risk, today


def demo_state():
    rng = random.Random(42)
    end = date.fromisoformat(today()) - timedelta(days=1)
    days = []
    while len(days) < 160:
        if end.weekday() < 5:
            days.append(end.isoformat())
        end -= timedelta(days=1)
    days.reverse()
    definitions = [("sh600519", "示例·消费", "消费", 100, 1450, 1530, .8),
                   ("sz300750", "示例·新能源", "新能源", 500, 205, 224, 1.3),
                   ("sh600036", "示例·银行", "银行", 2000, 34, 38, .7),
                   ("sh601318", "示例·保险", "保险", 1200, 46, 44, 1.1)]
    market = [rng.gauss(.0003, .012) for _ in days]
    histories, quotes, holdings = {}, {}, []
    for code, name, industry, quantity, cost, target, beta in definitions + [("sh000300", "基准", "", 0, 0, 4000, 1)]:
        price, series = 100., []
        for day, change in zip(days, market):
            price *= 1 + beta*change + (rng.gauss(0, .009) if code != "sh000300" else 0)
            series.append(dict(date=day, close=price, volume=100000))
        factor = target / series[-1]["close"]
        for row in series:
            row["close"] *= factor
        histories[code] = series
        if code != "sh000300":
            quotes[code] = dict(price=target, date=days[-1], source="合成演示数据", fetched_at="")
            holdings.append(dict(symbol=code, name=name, industry=industry, quantity=quantity,
                                 cost=cost, effective_date=days[0]))
    summary = overview(holdings, quotes, 65000)
    return dict(summary=summary, risk=risk(summary, histories, "sh000300"),
                settings=dict(cash="65000", benchmark="sh000300"), snapshots=[], series_meta=[],
                today=today(), demo=True)
