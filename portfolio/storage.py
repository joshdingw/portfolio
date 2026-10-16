"""SQLite storage; snapshots are immutable and edits are audited."""
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from .domain import BENCHMARKS, SHANGHAI, holding, iso_date, number, overview, risk, today


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              INSERT OR IGNORE INTO settings VALUES ('cash','0'),('benchmark','sh000300');
              CREATE TABLE IF NOT EXISTS holdings (symbol TEXT PRIMARY KEY, name TEXT NOT NULL,
                industry TEXT NOT NULL, quantity INTEGER NOT NULL, cost TEXT NOT NULL, effective_date TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS quotes (symbol TEXT PRIMARY KEY, price TEXT NOT NULL,
                date TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS history (symbol TEXT NOT NULL, date TEXT NOT NULL,
                close REAL NOT NULL, volume REAL NOT NULL, PRIMARY KEY(symbol,date));
              CREATE TABLE IF NOT EXISTS series_meta (symbol TEXT PRIMARY KEY, source TEXT, fetched_at TEXT);
              CREATE TABLE IF NOT EXISTS snapshots (date TEXT PRIMARY KEY, created_at TEXT, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at TEXT, action TEXT, payload TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def log(self, db, action, payload):
        db.execute("INSERT INTO audit(at,action,payload) VALUES (?,?,?)",
                   (datetime.now(SHANGHAI).isoformat(), action, json.dumps(payload, ensure_ascii=False)))

    def state(self):
        with self.connect() as db:
            holdings = [dict(r) for r in db.execute("SELECT * FROM holdings ORDER BY symbol")]
            quotes = {r["symbol"]: dict(r) for r in db.execute("SELECT * FROM quotes")}
            settings = dict(db.execute("SELECT key,value FROM settings"))
            histories = {}
            for r in db.execute("SELECT * FROM history ORDER BY date"):
                histories.setdefault(r["symbol"], []).append(dict(r))
            snapshots = [dict(date=r["date"], created_at=r["created_at"], **json.loads(r["payload"]))
                         for r in db.execute("SELECT * FROM snapshots ORDER BY date DESC LIMIT 60")]
            meta = [dict(r) for r in db.execute("SELECT * FROM series_meta")]
        summary = overview(holdings, quotes, settings["cash"])
        return dict(summary=summary, risk=risk(summary, histories, settings["benchmark"]),
                    settings=settings, snapshots=snapshots, series_meta=meta, today=today(), demo=False)

    def save_holdings(self, payloads):
        if not isinstance(payloads, list) or not 1 <= len(payloads) <= 200:
            raise ValueError("每次可保存1至200条持仓")
        rows = [holding(p) for p in payloads]
        if len({h["symbol"] for h in rows}) != len(rows):
            raise ValueError("同一批导入中存在重复股票代码，请先合并")
        with self.connect() as db:
            for h in rows:
                db.execute("INSERT INTO holdings VALUES (:symbol,:name,:industry,:quantity,:cost,:effective_date) "
                           "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,industry=excluded.industry,"
                           "quantity=excluded.quantity,cost=excluded.cost,effective_date=excluded.effective_date", h)
            self.log(db, "holdings_upsert", rows)

    def delete(self, code):
        with self.connect() as db:
            old = db.execute("SELECT * FROM holdings WHERE symbol=?", (code,)).fetchone()
            if not old:
                raise ValueError("未找到持仓")
            db.execute("DELETE FROM holdings WHERE symbol=?", (code,))
            self.log(db, "holding_delete", dict(old))

    def settings(self, payload):
        cash = str(number(payload.get("cash", ""), "现金余额"))
        benchmark = payload.get("benchmark", "sh000300")
        if benchmark not in BENCHMARKS:
            raise ValueError("不支持此基准")
        with self.connect() as db:
            db.execute("UPDATE settings SET value=? WHERE key='cash'", (cash,))
            db.execute("UPDATE settings SET value=? WHERE key='benchmark'", (benchmark,))
            self.log(db, "settings", dict(cash=cash, benchmark=benchmark))

    def manual_quote(self, payload):
        from .domain import symbol
        code = symbol(payload.get("symbol", ""))
        price = str(number(payload.get("price", ""), "价格", 0.000001, 1e7))
        day = iso_date(payload.get("date", ""))
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM holdings WHERE symbol=?", (code,)).fetchone():
                raise ValueError("请先录入该股票持仓")
            db.execute("INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?)",
                       (code, price, day, "手动价格", datetime.now(SHANGHAI).isoformat()))
            self.log(db, "manual_quote", payload)

    def save_market(self, code, quote, rows, source):
        stamp = datetime.now(SHANGHAI).isoformat()
        with self.connect() as db:
            # Replace an entire adjusted-price window: never splice differently rebased series.
            db.execute("DELETE FROM history WHERE symbol=?", (code,))
            db.executemany("INSERT INTO history VALUES (?,?,?,?)",
                           [(code, r["date"], r["close"], r["volume"]) for r in rows])
            db.execute("INSERT OR REPLACE INTO series_meta VALUES (?,?,?)", (code, source, stamp))
            if quote:
                existing = db.execute("SELECT date FROM quotes WHERE symbol=?", (code,)).fetchone()
                if not existing or existing["date"] <= quote["date"]:
                    db.execute("INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?)",
                               (code, str(quote["price"]), quote["date"], source, stamp))

    def snapshot(self):
        state = self.state()
        s = state["summary"]
        if not s["holdings"] or not s["complete"]:
            raise ValueError("请先录入持仓并补齐全部价格")
        dates = {h["quote"]["date"] for h in s["holdings"]}
        if len(dates) != 1:
            raise ValueError("持仓报价日期不一致，暂不保存快照")
        day = dates.pop()
        if any(h["effective_date"] > day for h in s["holdings"]):
            raise ValueError("报价日期早于持仓生效日期，不能回填历史账户快照")
        payload = dict(total=s["total"], stock_value=s["stock_value"], cash=s["cash"], pnl=s["pnl"],
                       holdings=s["holdings"], label="持仓估值快照，非账户收益曲线")
        with self.connect() as db:
            if db.execute("SELECT 1 FROM snapshots WHERE date=?", (day,)).fetchone():
                raise ValueError("该日期已有快照；保留原始记录，不覆盖")
            db.execute("INSERT INTO snapshots VALUES (?,?,?)",
                       (day, datetime.now(SHANGHAI).isoformat(), json.dumps(payload, ensure_ascii=False)))
            self.log(db, "snapshot", dict(date=day))
        return day

    def backup(self):
        with self.connect() as db:
            data = {table: [dict(r) for r in db.execute(f"SELECT * FROM {table}")]
                    for table in ("settings", "holdings", "quotes", "history", "series_meta", "snapshots", "audit")}
        return dict(schema_version=1, exported_at=datetime.now(SHANGHAI).isoformat(), tables=data)
