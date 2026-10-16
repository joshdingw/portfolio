"""Replaceable Tencent daily-bar adapter. No account information is transmitted."""
from datetime import datetime
import json
import math
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .domain import SHANGHAI, iso_date

SOURCE = "腾讯财经日线（非交易所直连）"
ENDPOINT = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def parse_rows(payload, code, adjusted, now=None):
    now = now or datetime.now(SHANGHAI)
    block = payload.get("data", {}).get(code, {})
    key = "qfqday" if adjusted else "day"
    # Tencent returns 'day' for indices and stocks without adjustments.
    raw = block.get(key) or block.get("day")
    if not isinstance(raw, list) or not raw:
        raise ValueError("数据源未返回有效日线，可能不支持此证券")
    result = {}
    for row in raw:
        if len(row) < 6:
            raise ValueError("日线字段不完整")
        day = iso_date(row[0])
        if day == now.date().isoformat() and (now.hour, now.minute) < (16, 0):
            continue
        close, volume = float(row[2]), float(row[5])
        if not math.isfinite(close) or not math.isfinite(volume) or close <= 0 or volume < 0:
            raise ValueError("历史价格或成交量异常，保留旧数据")
        result[day] = dict(date=day, close=close, volume=volume)
    if not result:
        raise ValueError("暂无已结束交易日的数据")
    return sorted(result.values(), key=lambda x: x["date"])


def fetch(code, adjusted):
    query = urlencode({"param": f"{code},day,,,320,{'qfq' if adjusted else ''}"})
    request = Request(ENDPOINT + "?" + query, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    with urlopen(request, timeout=12) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("行情响应过大")
    return parse_rows(json.loads(raw.decode("utf-8")), code, adjusted)


def fetch_security(code, index=False):
    adjusted = fetch(code, not index)
    if index:
        return None, adjusted
    raw = fetch(code, False)
    if raw[-1]["date"] != adjusted[-1]["date"]:
        raise ValueError("复权与原始行情日期不一致，保留旧数据")
    quote = dict(price=raw[-1]["close"], date=raw[-1]["date"])
    return quote, adjusted
