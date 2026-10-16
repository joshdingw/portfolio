"""Validation and reproducible portfolio calculations; no network or storage."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
import re
import statistics

SHANGHAI = timezone(timedelta(hours=8))
BENCHMARKS = {"sh000300": "沪深300", "sh000905": "中证500", "sh000852": "中证1000"}
METHOD = "v1 · 120日 · 前复权价格收益率近似 · 至少60个共同有效样本"


def today():
    return datetime.now(SHANGHAI).date().isoformat()


def number(value, label, minimum=0, maximum=1e12):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}必须是数字") from None
    if not result.is_finite() or not Decimal(str(minimum)) <= result <= Decimal(str(maximum)):
        raise ValueError(f"{label}应在 {minimum} 至 {maximum} 之间")
    return result


def iso_date(value):
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        raise ValueError("日期格式应为 YYYY-MM-DD") from None
    if parsed.isoformat() != value or parsed.isoformat() > today():
        raise ValueError("日期必须是有效的非未来日期（YYYY-MM-DD）")
    return value


def symbol(value):
    code = str(value).strip().lower()
    if re.fullmatch(r"\d{6}\.(sh|sz|bj)", code):
        code = code[-2:] + code[:6]
    if re.fullmatch(r"\d{6}", code):
        code = ("sh" if code.startswith("6") else "sz" if code.startswith(("0", "3")) else "bj") + code
    if not re.fullmatch(r"(?:sh6\d{5}|sz[03]\d{5}|bj[489]\d{5})", code):
        raise ValueError("请输入沪深京 A 股代码，例如 sh600519、000001.SZ、bj920001")
    return code


def holding(payload):
    code = symbol(payload.get("symbol", ""))
    quantity = number(payload.get("quantity", ""), "持仓数量", 1, 1e10)
    if quantity != quantity.to_integral_value():
        raise ValueError("股票数量必须是整数股")
    name = str(payload.get("name", "")).strip() or code.upper()
    industry = str(payload.get("industry", "")).strip() or "未分类"
    if len(name) > 40 or len(industry) > 40:
        raise ValueError("名称和行业最多40个字符")
    return dict(symbol=code, name=name, industry=industry, quantity=int(quantity),
                cost=str(number(payload.get("cost", ""), "平均成本", 0, 1e7)),
                effective_date=iso_date(payload.get("effective_date", today())))


def covariance(x, y):
    if len(x) < 2:
        return None
    mx, my = statistics.mean(x), statistics.mean(y)
    return sum((a-mx)*(b-my) for a, b in zip(x, y)) / (len(x)-1)


def correlation(x, y):
    vx, vy = covariance(x, x), covariance(y, y)
    if not vx or not vy or vx < 1e-18 or vy < 1e-18:
        return None
    return max(-1, min(1, covariance(x, y) / math.sqrt(vx*vy)))


def return_series(rows, calendar):
    """Require adjacent benchmark sessions; suspended/missing days are never filled."""
    prices = {r["date"]: r for r in rows}
    result = {}
    for previous, current in zip(calendar, calendar[1:]):
        a, b = prices.get(previous), prices.get(current)
        if a and b and a["close"] > 0 and b["close"] > 0 and a["volume"] > 0 and b["volume"] > 0:
            result[current] = b["close"] / a["close"] - 1
    return result


def overview(holdings, quotes, cash):
    cash = Decimal(str(cash))
    rows, priced_value, cost_total = [], Decimal(0), Decimal(0)
    missing = []
    for item in holdings:
        h = dict(item)
        q = quotes.get(h["symbol"])
        base = Decimal(str(h["cost"])) * h["quantity"]
        cost_total += base
        value = Decimal(str(q["price"])) * h["quantity"] if q else None
        if value is None:
            missing.append(h["symbol"])
        else:
            priced_value += value
        h.update(cost=float(h["cost"]), cost_value=float(base), quote=q,
                 value=float(value) if value is not None else None,
                 pnl=float(value-base) if value is not None else None,
                 pnl_rate=float((value-base)/base) if value is not None and base else None)
        rows.append(h)
    complete = not missing
    total = priced_value + cash
    industries = {}
    for h in rows:
        h["weight"] = h["value"]/float(total) if complete and total > 0 else None
        if h["value"] is not None:
            industries[h["industry"]] = industries.get(h["industry"], 0) + h["value"]
    weights = sorted([h["weight"] for h in rows if h["weight"] is not None], reverse=True)
    return dict(holdings=rows, cash=float(cash), total=float(total) if complete else None,
                stock_value=float(priced_value) if complete else None, priced_value=float(priced_value),
                cost_total=float(cost_total), pnl=float(priced_value-cost_total) if complete else None,
                cash_weight=float(cash/total) if complete and total else None,
                max_weight=weights[0] if weights else None, top5_weight=sum(weights[:5]) if weights else None,
                missing=missing, complete=complete, industries=industries)


def risk(summary, histories, benchmark):
    base_rows = histories.get(benchmark, [])
    calendar = sorted(r["date"] for r in base_rows)[-121:]
    base = return_series(base_rows, calendar)
    codes = [h["symbol"] for h in summary["holdings"]]
    returns = {s: return_series(histories.get(s, []), calendar) for s in codes}
    matrix = []
    for a in codes:
        line = []
        for b in codes:
            dates = sorted(set(returns[a]) & set(returns[b]))
            corr = correlation([returns[a][d] for d in dates], [returns[b][d] for d in dates]) if len(dates) >= 60 else None
            line.append(dict(value=corr, n=len(dates)))
        matrix.append(line)
    common = set(base)
    for series in returns.values():
        common &= set(series)
    dates = sorted(common) if codes else []
    beta = r2 = volatility = None
    reason = "需要至少60个共同有效交易日；请先更新行情。"
    if len(dates) >= 60 and summary["complete"] and summary["total"] > 0:
        y = [sum(h["weight"]*returns[h["symbol"]][d] for h in summary["holdings"]) for d in dates]
        x = [base[d] for d in dates]
        vx = covariance(x, x)
        if vx and vx > 1e-18:
            beta = covariance(x, y) / vx
            corr = correlation(x, y)
            r2 = corr*corr if corr is not None else None
            volatility = math.sqrt(max(0, covariance(y, y))*252)
            reason = "当前市值权重固定、现金收益按0估计；非历史账户表现。"
    elif not summary["complete"]:
        reason = "部分持仓缺少价格，无法确定账户权重。"
    return dict(codes=codes, matrix=matrix, beta=beta, r2=r2, volatility=volatility,
                n=len(dates), start=dates[0] if dates else None, end=dates[-1] if dates else None,
                benchmark=benchmark, benchmark_name=BENCHMARKS[benchmark], reason=reason, method=METHOD)
