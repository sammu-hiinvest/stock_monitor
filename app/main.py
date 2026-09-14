"""FastAPI 後端：提供資料 API，並直接把 static/index.html 當作儀表板首頁。"""

import os
from datetime import date, datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import DATA_DIR, WATCHLIST
from app.db import get_connection, init_db
from app.fetch_bonds import BOND_SERIES
from app.scrapers.stock_price import ensure_price_history

FETCH_LOG_PATH = os.path.join(DATA_DIR, "fetch_task.log")


def _log_fetch_line(line: str) -> None:
    """寫一行到跟排程共用的 data/fetch_task.log，讓手動抓取也留下紀錄可查。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(FETCH_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} [MANUAL] {line}\n")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="台股主動式ETF持股追蹤")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/funds")
def list_funds():
    conn = get_connection()
    try:
        out = []
        for etf in WATCHLIST:
            row = conn.execute(
                """
                SELECT date, nav_per_unit, total_units, units_diff, net_asset_value
                FROM daily_summary WHERE ticker = ? ORDER BY date DESC LIMIT 1
                """,
                (etf.ticker,),
            ).fetchone()
            out.append(
                {
                    "ticker": etf.ticker,
                    "name": etf.name,
                    "issuer": etf.issuer,
                    "latest_date": row["date"] if row else None,
                    "nav_per_unit": row["nav_per_unit"] if row else None,
                    "total_units": row["total_units"] if row else None,
                    "units_diff": row["units_diff"] if row else None,
                    "net_asset_value": row["net_asset_value"] if row else None,
                }
            )
        return out
    finally:
        conn.close()


def _require_ticker(ticker: str):
    if ticker not in {e.ticker for e in WATCHLIST}:
        raise HTTPException(status_code=404, detail=f"未追蹤的標的: {ticker}")


@app.get("/api/funds/{ticker}/summary")
def fund_summary(ticker: str, days: int = 180):
    _require_ticker(ticker)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT date, nav_per_unit, total_units, units_diff, net_asset_value, beneficiaries_count
            FROM daily_summary WHERE ticker = ?
            ORDER BY date DESC LIMIT ?
            """,
            (ticker, days),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


@app.get("/api/funds/{ticker}/dates")
def fund_dates(ticker: str):
    _require_ticker(ticker)
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM holdings WHERE ticker = ? ORDER BY date DESC",
            (ticker,),
        ).fetchall()
        return [r["date"] for r in rows]
    finally:
        conn.close()


@app.get("/api/funds/{ticker}/holdings")
def fund_holdings(ticker: str, date: str | None = None):
    _require_ticker(ticker)
    conn = get_connection()
    try:
        if date is None:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM holdings WHERE ticker = ?", (ticker,)
            ).fetchone()
            date = row["d"] if row else None
        if date is None:
            return {"date": None, "holdings": []}

        rows = conn.execute(
            """
            SELECT asset_type, code, name, shares, weight_pct, market_value
            FROM holdings WHERE ticker = ? AND date = ?
            ORDER BY weight_pct DESC
            """,
            (ticker, date),
        ).fetchall()
        return {"date": date, "holdings": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/funds/{ticker}/holdings/daily-changes")
def fund_daily_changes(ticker: str, date: str | None = None):
    """ETF 層級：某一天相對於「前一個有資料的交易日」的持股變動摘要。
    新增/剔除以個股是否還在清單內判斷；加碼/減碼以股數的絕對增減判斷 (不看權重%)。
    """
    _require_ticker(ticker)
    conn = get_connection()
    try:
        if date is None:
            row = conn.execute("SELECT MAX(date) AS d FROM holdings WHERE ticker = ?", (ticker,)).fetchone()
            date = row["d"] if row else None
        if date is None:
            return {"date": None, "prev_date": None, "added": [], "removed": [], "increased": [], "decreased": []}

        prev_row = conn.execute(
            "SELECT MAX(date) AS d FROM holdings WHERE ticker = ? AND date < ?", (ticker, date)
        ).fetchone()
        prev_date = prev_row["d"] if prev_row else None

        def load(d: str | None) -> dict[str, dict]:
            if d is None:
                return {}
            rows = conn.execute(
                "SELECT code, name, shares, weight_pct FROM holdings "
                "WHERE ticker = ? AND date = ? AND asset_type = 'STOCK'",
                (ticker, d),
            ).fetchall()
            return {r["code"]: dict(r) for r in rows}

        cur, prev = load(date), load(prev_date)
        added, removed, increased, decreased = [], [], [], []

        for code, row in cur.items():
            if code not in prev:
                added.append(row)
                continue
            prev_shares = prev[code]["shares"] or 0
            cur_shares = row["shares"] or 0
            diff = cur_shares - prev_shares
            if diff > 0:
                increased.append({**row, "prev_shares": prev_shares, "share_diff": diff})
            elif diff < 0:
                decreased.append({**row, "prev_shares": prev_shares, "share_diff": diff})

        for code, row in prev.items():
            if code not in cur:
                removed.append(row)

        increased.sort(key=lambda r: r["share_diff"], reverse=True)
        decreased.sort(key=lambda r: r["share_diff"])

        return {
            "date": date,
            "prev_date": prev_date,
            "added": added,
            "removed": removed,
            "increased": increased,
            "decreased": decreased,
        }
    finally:
        conn.close()


@app.get("/api/funds/{ticker}/stocks")
def fund_stock_list(ticker: str):
    """回傳這檔基金歷來持有過的個股清單 (供下拉選單用)，
    依「最新一次出現時的持股權重」由高到低排序，最新持股排前面。
    """
    _require_ticker(ticker)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT h.code, h.name, h.date, h.weight_pct
            FROM holdings h
            INNER JOIN (
                SELECT code, MAX(date) AS max_date
                FROM holdings WHERE ticker = ? AND asset_type = 'STOCK'
                GROUP BY code
            ) latest ON h.code = latest.code AND h.date = latest.max_date
            WHERE h.ticker = ? AND h.asset_type = 'STOCK'
            ORDER BY h.date DESC, h.weight_pct DESC
            """,
            (ticker, ticker),
        ).fetchall()
        return [{"code": r["code"], "name": r["name"]} for r in rows]
    finally:
        conn.close()


@app.get("/api/funds/{ticker}/stocks/{code}/history")
def fund_stock_history(ticker: str, code: str):
    """回傳單一個股在這檔基金裡，逐日的持股股數與持股權重走勢。
    沒有持有的日期會補 null，讓走勢圖能畫出進出場的斷點。
    """
    _require_ticker(ticker)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT d.date AS date, h.shares AS shares, h.weight_pct AS weight_pct
            FROM (SELECT DISTINCT date FROM holdings WHERE ticker = ?) d
            LEFT JOIN holdings h
                ON h.ticker = ? AND h.date = d.date AND h.code = ? AND h.asset_type = 'STOCK'
            ORDER BY d.date ASC
            """,
            (ticker, ticker, code),
        ).fetchall()
        name_row = conn.execute(
            "SELECT name FROM holdings WHERE ticker = ? AND code = ? AND asset_type = 'STOCK' "
            "ORDER BY date DESC LIMIT 1",
            (ticker, code),
        ).fetchone()
        return {
            "code": code,
            "name": name_row["name"] if name_row else code,
            "series": [dict(r) for r in rows],
        }
    finally:
        conn.close()


@app.get("/api/stocks")
def all_stocks_list():
    """跨所有追蹤 ETF，列出歷來出現過的個股 (供「個股跨ETF比較」頁的選單用)，
    依「目前有幾檔 ETF 持有」由高到低排序。
    """
    conn = get_connection()
    try:
        latest_rows = conn.execute(
            """
            SELECT code, name, date AS latest_date FROM (
                SELECT code, name, date,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                FROM holdings WHERE asset_type = 'STOCK'
            ) WHERE rn = 1
            """
        ).fetchall()
        counts = {
            r["code"]: r["c"]
            for r in conn.execute(
                "SELECT code, COUNT(DISTINCT ticker) AS c FROM holdings WHERE asset_type = 'STOCK' GROUP BY code"
            ).fetchall()
        }
        out = [
            {
                "code": r["code"],
                "name": r["name"],
                "latest_date": r["latest_date"],
                "etf_count": counts.get(r["code"], 0),
            }
            for r in latest_rows
        ]
        out.sort(key=lambda r: (-(r["etf_count"] or 0), r["code"]))
        return out
    finally:
        conn.close()


def _stock_actions_for_ticker(conn, ticker: str, code: str) -> list[dict]:
    """逐日比較這檔 ETF 對某支個股的持股股數，把每一次變動分類成：
    新增(0→有)、加碼(增加)、減碼(減少但未歸零)、出清(有→0)。
    第一筆資料日沒有前一天可比較，不視為事件(避免把「基金一開始就持有」誤判成新增)。
    """
    rows = conn.execute(
        """
        SELECT d.date AS date, h.shares AS shares
        FROM (SELECT DISTINCT date FROM holdings WHERE ticker = ?) d
        LEFT JOIN holdings h
            ON h.ticker = ? AND h.date = d.date AND h.code = ? AND h.asset_type = 'STOCK'
        ORDER BY d.date ASC
        """,
        (ticker, ticker, code),
    ).fetchall()

    actions = []
    prev_shares = None
    for r in rows:
        cur_shares = r["shares"] or 0
        if prev_shares is not None and cur_shares != prev_shares:
            if prev_shares == 0 and cur_shares > 0:
                action = "新增"
            elif prev_shares > 0 and cur_shares == 0:
                action = "出清"
            elif cur_shares > prev_shares:
                action = "加碼"
            else:
                action = "減碼"
            actions.append(
                {
                    "date": r["date"],
                    "action": action,
                    "shares": cur_shares,
                    "prev_shares": prev_shares,
                    "share_diff": cur_shares - prev_shares,
                }
            )
        prev_shares = cur_shares
    return actions


@app.get("/api/stocks/{code}/events")
def stock_events(code: str):
    """逐日比對每一檔追蹤 ETF 對這支個股的持股股數變化，彙總到單一個股層面，
    依時間序回傳 新增/加碼/減碼/出清 事件清單 (可能同一天有多檔 ETF 各自的動作)。
    """
    conn = get_connection()
    try:
        events = []
        for etf in WATCHLIST:
            held_any = conn.execute(
                "SELECT 1 FROM holdings WHERE ticker = ? AND code = ? AND asset_type = 'STOCK' LIMIT 1",
                (etf.ticker, code),
            ).fetchone()
            if not held_any:
                continue
            for action in _stock_actions_for_ticker(conn, etf.ticker, code):
                events.append({"ticker": etf.ticker, "fund_name": etf.name, **action})

        events.sort(key=lambda e: (e["date"], e["ticker"]))
        return events
    finally:
        conn.close()


@app.get("/api/stocks/{code}/price")
def stock_price_history(code: str):
    """個股收盤價走勢 (第一次查詢某代碼時會即時向 TWSE/TPEx 補抓資料並快取)。"""
    conn = get_connection()
    try:
        span = conn.execute(
            "SELECT MIN(date) AS mn, MAX(date) AS mx FROM holdings WHERE code = ? AND asset_type = 'STOCK'",
            (code,),
        ).fetchone()
        if not span or not span["mn"]:
            return {"code": code, "series": [], "unavailable": False}

        start_date = span["mn"]
        end_date = max(span["mx"], date.today().isoformat())

        ensure_price_history(conn, code, start_date, end_date)
        conn.commit()

        rows = conn.execute(
            "SELECT date, close FROM stock_prices WHERE code = ? AND date >= ? ORDER BY date ASC",
            (code, start_date),
        ).fetchall()
        return {"code": code, "series": [dict(r) for r in rows], "unavailable": len(rows) == 0}
    finally:
        conn.close()


@app.post("/api/fetch-now")
def fetch_now():
    """手動立即觸發一次抓取 (與排程用的是同一份程式)，並把結果寫進
    data/fetch_task.log，跟排程共用同一份紀錄檔方便事後查閱。
    """
    from app.fetch import run_all
    from app.fetch_acm import run_latest as run_acm_latest
    from app.fetch_auctions import run_latest as run_auctions_latest
    from app.fetch_bonds import run_latest as run_bonds_latest
    from app.fetch_cpi import run_latest as run_cpi_latest
    from app.fetch_futures import run_latest as run_futures_latest
    from app.fetch_macro import run_latest as run_macro_latest
    from app.fetch_mpt import run_latest as run_mpt_latest
    from app.fetch_txo import run_latest as run_txo_latest

    _log_fetch_line("===== 手動觸發抓取開始 (透過網頁「立即抓取最新資料」按鈕) =====")

    result = run_all()
    ok_count = sum(1 for v in result.values() if v == "ok")
    _log_fetch_line(f"ETF 持股：{ok_count}/{len(result)} 檔成功 -> {result}")

    try:
        result["_txo"] = run_txo_latest()
        _log_fetch_line(f"台指選擇權(TXO)：{result['_txo']}")
    except Exception as exc:  # noqa: BLE001
        result["_txo"] = f"error: {exc}"
        _log_fetch_line(f"台指選擇權(TXO) 失敗：{exc}")

    try:
        result["_futures"] = run_futures_latest()
        _log_fetch_line(f"大盤期貨：{result['_futures']}")
    except Exception as exc:  # noqa: BLE001
        result["_futures"] = f"error: {exc}"
        _log_fetch_line(f"大盤期貨 失敗：{exc}")

    try:
        result["_bonds"] = run_bonds_latest()
        _log_fetch_line(f"債市：{result['_bonds']}")
    except Exception as exc:  # noqa: BLE001
        result["_bonds"] = f"error: {exc}"
        _log_fetch_line(f"債市 失敗：{exc}")

    try:
        result["_mpt"] = run_mpt_latest()
        _log_fetch_line(f"Atlanta Fed 政策利率機率：{result['_mpt']}")
    except Exception as exc:  # noqa: BLE001
        result["_mpt"] = f"error: {exc}"
        _log_fetch_line(f"Atlanta Fed 政策利率機率 失敗：{exc}")

    try:
        result["_cpi"] = run_cpi_latest()
        _log_fetch_line(f"美國CPI(BLS)：{result['_cpi']}")
    except Exception as exc:  # noqa: BLE001
        result["_cpi"] = f"error: {exc}"
        _log_fetch_line(f"美國CPI(BLS) 失敗：{exc}")

    try:
        result["_macro"] = run_macro_latest()
        _log_fetch_line(f"美國總經數據(BLS)：{result['_macro']}")
    except Exception as exc:  # noqa: BLE001
        result["_macro"] = f"error: {exc}"
        _log_fetch_line(f"美國總經數據(BLS) 失敗：{exc}")

    try:
        result["_acm"] = run_acm_latest()
        _log_fetch_line(f"NY Fed ACM Term Premium：{result['_acm']}")
    except Exception as exc:  # noqa: BLE001
        result["_acm"] = f"error: {exc}"
        _log_fetch_line(f"NY Fed ACM Term Premium 失敗：{exc}")

    try:
        result["_auctions"] = run_auctions_latest()
        _log_fetch_line(f"美債標售：{result['_auctions']}")
    except Exception as exc:  # noqa: BLE001
        result["_auctions"] = f"error: {exc}"
        _log_fetch_line(f"美債標售 失敗：{exc}")

    _log_fetch_line("===== 手動觸發抓取結束 =====")
    return result


# ---------------------------------------------------------------------------
# 台指選擇權 (TXO)
# ---------------------------------------------------------------------------


@app.get("/api/txo/dates")
def txo_dates():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM txo_daily WHERE session = 'REGULAR' ORDER BY trade_date DESC"
        ).fetchall()
        return [r["trade_date"] for r in rows]
    finally:
        conn.close()


@app.get("/api/txo/contract-months")
def txo_contract_months():
    """回傳所有已下載資料裡出現過的到期月份(週別)，依字串排序 (同年月的月選/週選會排在一起)。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT contract_month FROM txo_daily WHERE session = 'REGULAR' ORDER BY contract_month ASC"
        ).fetchall()
        return [r["contract_month"] for r in rows]
    finally:
        conn.close()


@app.get("/api/txo/summary")
def txo_summary(start: str, end: str, contract_month: str | None = None):
    """每日整體市場 (或指定到期月份) 的 Put/Call 未平倉量、成交量彙總，供合併走勢圖用。"""
    conn = get_connection()
    try:
        sql = """
            SELECT trade_date AS date,
                   SUM(CASE WHEN option_type = 'C' THEN open_interest ELSE 0 END) AS call_oi,
                   SUM(CASE WHEN option_type = 'P' THEN open_interest ELSE 0 END) AS put_oi,
                   SUM(CASE WHEN option_type = 'C' THEN volume ELSE 0 END) AS call_volume,
                   SUM(CASE WHEN option_type = 'P' THEN volume ELSE 0 END) AS put_volume
            FROM txo_daily
            WHERE session = 'REGULAR' AND trade_date BETWEEN ? AND ?
        """
        params: list = [start, end]
        if contract_month:
            sql += " AND contract_month = ?"
            params.append(contract_month)
        sql += " GROUP BY trade_date ORDER BY trade_date ASC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/txo/oi-range")
def txo_oi_range(start: str, end: str, contract_month: str | None = None):
    """每日 Put/Call 未平倉量最大的履約價 (支撐/壓力關鍵價位)，供橫向區間軌跡圖用。
    contract_month 留空 = 「全部合約」：同一履約價會先加總所有到期月份的未平倉量，
    再從加總後的結果找最大值那個履約價。
    """
    conn = get_connection()
    try:
        def max_strike_by_date(option_type: str) -> dict[str, dict]:
            if contract_month:
                rows = conn.execute(
                    """
                    SELECT trade_date, strike_price, open_interest FROM (
                        SELECT trade_date, strike_price, open_interest,
                               ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY open_interest DESC) AS rn
                        FROM txo_daily
                        WHERE session = 'REGULAR' AND option_type = ? AND contract_month = ?
                              AND trade_date BETWEEN ? AND ?
                    ) WHERE rn = 1
                    """,
                    (option_type, contract_month, start, end),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT trade_date, strike_price, open_interest FROM (
                        SELECT trade_date, strike_price, SUM(open_interest) AS open_interest,
                               ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY SUM(open_interest) DESC) AS rn
                        FROM txo_daily
                        WHERE session = 'REGULAR' AND option_type = ? AND trade_date BETWEEN ? AND ?
                        GROUP BY trade_date, strike_price
                    ) WHERE rn = 1
                    """,
                    (option_type, start, end),
                ).fetchall()
            return {r["trade_date"]: dict(r) for r in rows}

        calls = max_strike_by_date("C")
        puts = max_strike_by_date("P")
        dates = sorted(set(calls) | set(puts))

        out = []
        for d in dates:
            c, p = calls.get(d), puts.get(d)
            out.append(
                {
                    "date": d,
                    "call_max_strike": c["strike_price"] if c else None,
                    "call_max_oi": c["open_interest"] if c else None,
                    "put_max_strike": p["strike_price"] if p else None,
                    "put_max_oi": p["open_interest"] if p else None,
                }
            )
        return out
    finally:
        conn.close()


@app.get("/api/txo/compare")
def txo_compare(date_a: str, date_b: str, contract_month: str | None = None):
    """兩個日期，逐履約價比較 Put/Call 未平倉量與成交量。
    contract_month 留空 = 「全部合約」：同一履約價加總所有到期月份後再比較。
    """
    conn = get_connection()
    try:
        if contract_month:
            rows = conn.execute(
                """
                SELECT trade_date, strike_price, option_type, open_interest, volume
                FROM txo_daily
                WHERE session = 'REGULAR' AND contract_month = ? AND trade_date IN (?, ?)
                """,
                (contract_month, date_a, date_b),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT trade_date, strike_price, option_type,
                       SUM(open_interest) AS open_interest, SUM(volume) AS volume
                FROM txo_daily
                WHERE session = 'REGULAR' AND trade_date IN (?, ?)
                GROUP BY trade_date, strike_price, option_type
                """,
                (date_a, date_b),
            ).fetchall()

        by_strike: dict[float, dict] = {}
        for r in rows:
            entry = by_strike.setdefault(r["strike_price"], {"strike": r["strike_price"]})
            tag = "a" if r["trade_date"] == date_a else "b"
            side = "call" if r["option_type"] == "C" else "put"
            entry[f"{side}_oi_{tag}"] = r["open_interest"]
            entry[f"{side}_volume_{tag}"] = r["volume"]

        out = sorted(by_strike.values(), key=lambda e: e["strike"])
        return {"date_a": date_a, "date_b": date_b, "contract_month": contract_month, "rows": out}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 大盤期貨 (TX/MTX/TMF + 三大法人 + 反推散戶)
# ---------------------------------------------------------------------------

# MTX(小型臺指)、TMF(微型臺指) 換算成 TX(臺股期貨)約當口數的倍數，
# 由期交所公布的契約金額回推驗證：TX=200元/點、MTX=50元/點、TMF=10元/點。
TX_EQUIV = {"TX": 1.0, "MTX": 0.25, "TMF": 0.05}
INVESTOR_LABELS = {"DEALER": "自營商", "TRUST": "投信", "FOREIGN": "外資及陸資", "RETAIL": "散戶(反推)"}


def _futures_positions_for_date(conn, trade_date: str) -> dict:
    """回傳 {product: {DEALER/TRUST/FOREIGN/RETAIL: {long_oi, short_oi, net_oi, long_volume, short_volume, net_volume}}}。
    散戶部位 = 市場整體(全部到期月份加總、一般盤) - 三大法人合計，多空兩邊分別計算。
    """
    total_oi_by_product = {
        r["product"]: r["total_oi"]
        for r in conn.execute(
            "SELECT product, SUM(open_interest) AS total_oi FROM futures_daily "
            "WHERE trade_date = ? AND session = 'REGULAR' GROUP BY product",
            (trade_date,),
        ).fetchall()
    }
    total_volume_by_product = {
        r["product"]: r["total_volume"]
        for r in conn.execute(
            "SELECT product, SUM(volume) AS total_volume FROM futures_daily "
            "WHERE trade_date = ? AND session = 'REGULAR' GROUP BY product",
            (trade_date,),
        ).fetchall()
    }
    inst_rows = conn.execute(
        "SELECT product, investor_type, long_oi, short_oi, long_volume, short_volume "
        "FROM institutional_futures WHERE trade_date = ?",
        (trade_date,),
    ).fetchall()

    out: dict[str, dict] = {}
    inst_sum_oi: dict[str, list] = {}
    inst_sum_vol: dict[str, list] = {}
    for r in inst_rows:
        p = r["product"]
        out.setdefault(p, {})[r["investor_type"]] = {
            "long_oi": r["long_oi"] or 0,
            "short_oi": r["short_oi"] or 0,
            "long_volume": r["long_volume"] or 0,
            "short_volume": r["short_volume"] or 0,
        }
        s = inst_sum_oi.setdefault(p, [0, 0])
        s[0] += r["long_oi"] or 0
        s[1] += r["short_oi"] or 0
        sv = inst_sum_vol.setdefault(p, [0, 0])
        sv[0] += r["long_volume"] or 0
        sv[1] += r["short_volume"] or 0

    for p, total_oi in total_oi_by_product.items():
        long_sum, short_sum = inst_sum_oi.get(p, [0, 0])
        vol_long_sum, vol_short_sum = inst_sum_vol.get(p, [0, 0])
        total_vol = total_volume_by_product.get(p, 0) or 0
        out.setdefault(p, {})["RETAIL"] = {
            "long_oi": (total_oi or 0) - long_sum,
            "short_oi": (total_oi or 0) - short_sum,
            "long_volume": total_vol - vol_long_sum,
            "short_volume": total_vol - vol_short_sum,
        }

    for investors in out.values():
        for v in investors.values():
            v["net_oi"] = v["long_oi"] - v["short_oi"]
            v["net_volume"] = v["long_volume"] - v["short_volume"]
    return out


@app.get("/api/futures/dates")
def futures_dates():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM futures_daily WHERE session = 'REGULAR' ORDER BY trade_date DESC"
        ).fetchall()
        return [r["trade_date"] for r in rows]
    finally:
        conn.close()


@app.get("/api/futures/table")
def futures_table(date: str | None = None):
    """三大法人(+反推散戶) 在 TX/MTX/TMF 的淨未平倉口數，換算成 TX 約當口數，
    並附上跟前一個交易日的變化，供「大盤期貨」頁的比較表格用。
    """
    conn = get_connection()
    try:
        if date is None:
            row = conn.execute(
                "SELECT MAX(trade_date) AS d FROM futures_daily WHERE session = 'REGULAR'"
            ).fetchone()
            date = row["d"] if row else None
        if date is None:
            return {"date": None, "prev_date": None, "rows": []}

        prev_row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM futures_daily WHERE session = 'REGULAR' AND trade_date < ?",
            (date,),
        ).fetchone()
        prev_date = prev_row["d"] if prev_row and prev_row["d"] else None

        cur = _futures_positions_for_date(conn, date)
        prev = _futures_positions_for_date(conn, prev_date) if prev_date else {}

        rows = []
        for investor_type, label in INVESTOR_LABELS.items():
            entry = {"investor_type": investor_type, "label": label}
            total_txeq = 0.0
            prev_total_txeq = 0.0
            for product, mult in TX_EQUIV.items():
                cur_net = cur.get(product, {}).get(investor_type, {}).get("net_oi")
                prev_net = prev.get(product, {}).get(investor_type, {}).get("net_oi")
                entry[f"{product.lower()}_net_oi"] = cur_net
                entry[f"{product.lower()}_net_oi_txeq"] = None if cur_net is None else round(cur_net * mult, 1)
                entry[f"{product.lower()}_change_txeq"] = (
                    round(((cur_net or 0) - (prev_net or 0)) * mult, 1) if prev_date else None
                )
                total_txeq += (cur_net or 0) * mult
                prev_total_txeq += (prev_net or 0) * mult
            entry["total_txeq"] = round(total_txeq, 1)
            entry["change_txeq"] = round(total_txeq - prev_total_txeq, 1) if prev_date else None
            rows.append(entry)

        return {"date": date, "prev_date": prev_date, "rows": rows}
    finally:
        conn.close()


@app.get("/api/futures/distribution")
def futures_distribution(product: str, date: str | None = None):
    """指定商品(MTX/TMF/TX) 在某交易日，各類投資人(含反推散戶)的多空未平倉分布，
    供「大盤期貨」頁的圓餅/長條圖 + 明細表用。
    """
    if product not in TX_EQUIV:
        raise HTTPException(status_code=400, detail=f"未知的期貨商品: {product}")
    conn = get_connection()
    try:
        if date is None:
            row = conn.execute(
                "SELECT MAX(trade_date) AS d FROM futures_daily WHERE session = 'REGULAR'"
            ).fetchone()
            date = row["d"] if row else None
        if date is None:
            return {"date": None, "product": product, "rows": []}

        positions = _futures_positions_for_date(conn, date).get(product, {})
        rows = []
        for investor_type, label in INVESTOR_LABELS.items():
            v = positions.get(investor_type, {})
            long_oi = v.get("long_oi", 0) or 0
            short_oi = v.get("short_oi", 0) or 0
            total = long_oi + short_oi
            rows.append(
                {
                    "investor_type": investor_type,
                    "label": label,
                    "long_oi": long_oi,
                    "short_oi": short_oi,
                    "net_oi": long_oi - short_oi,
                    "long_pct": round(long_oi / total * 100, 1) if total else None,
                    "short_pct": round(short_oi / total * 100, 1) if total else None,
                }
            )
        return {"date": date, "product": product, "rows": rows}
    finally:
        conn.close()


@app.get("/api/futures/net-ratio")
def futures_net_ratio(start: str, end: str, product: str):
    """各類投資人(含反推散戶) 未平倉淨多空比率的歷史走勢。
    公式：(多單口數 - 空單口數) / 當日整體未平倉量 * 100%
    """
    if product not in TX_EQUIV:
        raise HTTPException(status_code=400, detail=f"未知的期貨商品: {product}")
    conn = get_connection()
    try:
        dates = [
            r["trade_date"]
            for r in conn.execute(
                "SELECT DISTINCT trade_date FROM futures_daily "
                "WHERE session = 'REGULAR' AND product = ? AND trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date ASC",
                (product, start, end),
            ).fetchall()
        ]

        series: dict[str, list] = {k: [] for k in INVESTOR_LABELS}
        for d in dates:
            total_row = conn.execute(
                "SELECT SUM(open_interest) AS t FROM futures_daily "
                "WHERE trade_date = ? AND product = ? AND session = 'REGULAR'",
                (d, product),
            ).fetchone()
            total_oi = total_row["t"] if total_row else None
            positions = _futures_positions_for_date(conn, d).get(product, {})
            for investor_type in INVESTOR_LABELS:
                net = positions.get(investor_type, {}).get("net_oi")
                if total_oi:
                    series[investor_type].append(round((net or 0) / total_oi * 100, 2))
                else:
                    series[investor_type].append(None)

        return {"product": product, "dates": dates, "series": series, "labels": INVESTOR_LABELS}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 債市 (美債殖利率曲線 / 利差 / 公司債利差 / 5Y5Y遠期通膨預期)
# ---------------------------------------------------------------------------


def _bond_nearest(conn, series_id: str, on_or_before: str):
    """回傳 series_id 在 on_or_before(含) 之前最近一筆的 (obs_date, value)。"""
    row = conn.execute(
        "SELECT obs_date, value FROM bond_series WHERE series_id = ? AND obs_date <= ? "
        "ORDER BY obs_date DESC LIMIT 1",
        (series_id, on_or_before),
    ).fetchone()
    return (row["obs_date"], row["value"]) if row else (None, None)


def _percentile_rank(values: list[float], latest_value: float) -> float | None:
    """latest_value 落在 values(含自己) 裡的百分位 (0~100)：<= latest_value 的比例。"""
    if not values:
        return None
    count_le = sum(1 for v in values if v <= latest_value)
    return round(count_le / len(values) * 100, 1)


def _percentile_of_series(conn, series_id: str, years: int = 2) -> float | None:
    """該序列最新值相較過去 N 年歷史的百分位 (0~100)。"""
    row = conn.execute(
        "SELECT obs_date, value FROM bond_series WHERE series_id = ? ORDER BY obs_date DESC LIMIT 1",
        (series_id,),
    ).fetchone()
    if not row:
        return None
    cutoff = (date.fromisoformat(row["obs_date"]) - timedelta(days=365 * years)).isoformat()
    hist_rows = conn.execute(
        "SELECT value FROM bond_series WHERE series_id = ? AND obs_date >= ? AND obs_date <= ?",
        (series_id, cutoff, row["obs_date"]),
    ).fetchall()
    return _percentile_rank([r["value"] for r in hist_rows], row["value"])


def _hy_minus_bbb_series(conn) -> list[tuple[str, float]]:
    """回傳 (obs_date, 高收益債利差 - BBB投等利差) 時間序列，只保留兩邊都有資料的日期。"""
    bbb_rows = conn.execute(
        "SELECT obs_date, value FROM bond_series WHERE series_id = 'BAMLC0A4CBBB' ORDER BY obs_date",
    ).fetchall()
    hy_rows = conn.execute(
        "SELECT obs_date, value FROM bond_series WHERE series_id = 'BAMLH0A0HYM2' ORDER BY obs_date",
    ).fetchall()
    bbb_map = {r["obs_date"]: r["value"] for r in bbb_rows}
    hy_map = {r["obs_date"]: r["value"] for r in hy_rows}
    dates = sorted(set(bbb_map) & set(hy_map))
    return [(d, hy_map[d] - bbb_map[d]) for d in dates]


@app.get("/api/bonds/yield-curve")
def bonds_yield_curve():
    """最新殖利率曲線，附上跟 1週/1個月/3個月/年初至今 相比的 bps 變化。"""
    conn = get_connection()
    try:
        maturities = sorted(
            ((sid, label) for sid, (label, cat, _order) in BOND_SERIES.items() if cat == "YIELD_CURVE"),
            key=lambda x: BOND_SERIES[x[0]][2],
        )
        if not maturities:
            return {"date": None, "rows": []}

        placeholders = ",".join("?" for _ in maturities)
        latest_row = conn.execute(
            f"SELECT MAX(obs_date) AS d FROM bond_series WHERE series_id IN ({placeholders})",
            [sid for sid, _ in maturities],
        ).fetchone()
        latest_date = latest_row["d"] if latest_row else None
        if not latest_date:
            return {"date": None, "rows": []}

        d = date.fromisoformat(latest_date)
        ref_1w = (d - timedelta(days=7)).isoformat()
        ref_1m = (d - timedelta(days=30)).isoformat()
        ref_3m = (d - timedelta(days=90)).isoformat()
        ref_ytd = date(d.year - 1, 12, 31).isoformat()

        def bps(a, b):
            return None if a is None or b is None else round((a - b) * 100, 1)

        rows = []
        for sid, label in maturities:
            _, latest_v = _bond_nearest(conn, sid, latest_date)
            _, v_1w = _bond_nearest(conn, sid, ref_1w)
            _, v_1m = _bond_nearest(conn, sid, ref_1m)
            _, v_3m = _bond_nearest(conn, sid, ref_3m)
            _, v_ytd = _bond_nearest(conn, sid, ref_ytd)
            rows.append(
                {
                    "series_id": sid,
                    "label": label,
                    "value": latest_v,
                    "value_1w": v_1w,
                    "value_1m": v_1m,
                    "value_3m": v_3m,
                    "value_ytd": v_ytd,
                    "chg_1w_bps": bps(latest_v, v_1w),
                    "chg_1m_bps": bps(latest_v, v_1m),
                    "chg_3m_bps": bps(latest_v, v_3m),
                    "chg_ytd_bps": bps(latest_v, v_ytd),
                }
            )
        return {
            "date": latest_date,
            "ref_dates": {"1w": ref_1w, "1m": ref_1m, "3m": ref_3m, "ytd": ref_ytd},
            "rows": rows,
        }
    finally:
        conn.close()


@app.get("/api/bonds/series")
def bonds_series(ids: str, start: str | None = None, end: str | None = None):
    """逗號分隔的多個 FRED series_id，回傳各自的歷史時間序列 (供利差/公司債走勢圖共用)。
    不帶 start/end 就回傳資料庫裡的全部歷史 (預設回溯抓 2 年)。
    """
    conn = get_connection()
    try:
        id_list = [s.strip() for s in ids.split(",") if s.strip()]
        out = {}
        for sid in id_list:
            sql = "SELECT obs_date, value FROM bond_series WHERE series_id = ?"
            params: list = [sid]
            if start:
                sql += " AND obs_date >= ?"
                params.append(start)
            if end:
                sql += " AND obs_date <= ?"
                params.append(end)
            sql += " ORDER BY obs_date ASC"
            rows = conn.execute(sql, params).fetchall()
            out[sid] = [{"date": r["obs_date"], "value": r["value"]} for r in rows]
        return out
    finally:
        conn.close()


@app.get("/api/bonds/latest")
def bonds_latest():
    """非殖利率曲線的幾個關鍵序列(利差/公司債利差/遠期通膨預期)最新值，供頁面頂部關鍵指標卡片用。"""
    conn = get_connection()
    try:
        out = []
        for sid, (label, category, _order) in BOND_SERIES.items():
            if category in ("YIELD_CURVE", "POLICY_RATE", "TAYLOR_INPUT", "CPI_INDEX", "PCE_INDEX", "CPI_SUB_INDEX"):
                continue
            row = conn.execute(
                "SELECT obs_date, value FROM bond_series WHERE series_id = ? ORDER BY obs_date DESC LIMIT 1",
                (sid,),
            ).fetchone()
            out.append(
                {
                    "series_id": sid,
                    "label": label,
                    "category": category,
                    "date": row["obs_date"] if row else None,
                    "value": row["value"] if row else None,
                    "percentile": _percentile_of_series(conn, sid) if row else None,
                }
            )

        # 額外新增一個衍生指標：高收益債 - BBB投等利差 (利差之利差，反映信用品質分層程度)
        diff_series = _hy_minus_bbb_series(conn)
        if diff_series:
            latest_date, latest_diff = diff_series[-1]
            cutoff = (date.fromisoformat(latest_date) - timedelta(days=365 * 2)).isoformat()
            hist_values = [v for d, v in diff_series if d >= cutoff]
            out.append(
                {
                    "series_id": "HY_MINUS_BBB",
                    "label": "高收益債－BBB投等利差",
                    "category": "CREDIT",
                    "date": latest_date,
                    "value": latest_diff,
                    "percentile": _percentile_rank(hist_values, latest_diff),
                }
            )

        # 額外新增：Atlanta Fed Market Probability Tracker 最新一天的升息/降息機率，
        # 附上跟前一個觀測日相比的變化(百分點)。
        mpt_rows = conn.execute(
            "SELECT obs_date, prob_cut, prob_hike FROM mpt_probability ORDER BY obs_date DESC LIMIT 2",
        ).fetchall()
        if mpt_rows:
            latest_mpt = mpt_rows[0]
            prev_mpt = mpt_rows[1] if len(mpt_rows) > 1 else None

            def delta_1d(field: str) -> float | None:
                if prev_mpt is None or latest_mpt[field] is None or prev_mpt[field] is None:
                    return None
                return round(latest_mpt[field] - prev_mpt[field], 2)

            out.append(
                {
                    "series_id": "MPT_PROB_CUT",
                    "label": "Atlanta Fed 降息機率",
                    "category": "MPT_PROB",
                    "date": latest_mpt["obs_date"],
                    "value": latest_mpt["prob_cut"],
                    "delta_1d": delta_1d("prob_cut"),
                    "percentile": None,
                }
            )
            out.append(
                {
                    "series_id": "MPT_PROB_HIKE",
                    "label": "Atlanta Fed 升息機率",
                    "category": "MPT_PROB",
                    "date": latest_mpt["obs_date"],
                    "value": latest_mpt["prob_hike"],
                    "delta_1d": delta_1d("prob_hike"),
                    "percentile": None,
                }
            )
        return out
    finally:
        conn.close()


@app.get("/api/bonds/policy-expectation")
def bonds_policy_expectation():
    """用短天期美債殖利率(3M/1Y)近似市場對未來政策利率的預期走勢，跟實際聯邦資金目標
    區間(上限/下限/中位數)畫在同一張時間序列圖上比較。

    嚴謹的 Fed funds futures 逐次FOMC會議機率分布(CME FedWatch)沒有免費、免金鑰的公開
    資料源可用(FRED 不提供期貨報價，CME 官方資料要付費訂閱、且有反爬蟲保護)。這裡改用
    「預期假說(expectations hypothesis)」的簡化近似：T-bill 到期殖利率 ≈ 市場預期在該
    期間內的平均無風險短率，所以跟目前政策利率的落差，可以粗略反映市場對未來升降息的
    預期方向與幅度——不是官方逐次會議機率分布，只是用現有免費資料做的近似值。
    """
    conn = get_connection()
    try:
        upper_rows = conn.execute(
            "SELECT obs_date, value FROM bond_series WHERE series_id = 'DFEDTARU' ORDER BY obs_date",
        ).fetchall()
        lower_rows = conn.execute(
            "SELECT obs_date, value FROM bond_series WHERE series_id = 'DFEDTARL' ORDER BY obs_date",
        ).fetchall()
        y3m_rows = conn.execute(
            "SELECT obs_date, value FROM bond_series WHERE series_id = 'DGS3MO' ORDER BY obs_date",
        ).fetchall()
        y1y_rows = conn.execute(
            "SELECT obs_date, value FROM bond_series WHERE series_id = 'DGS1' ORDER BY obs_date",
        ).fetchall()

        upper_map = {r["obs_date"]: r["value"] for r in upper_rows}
        lower_map = {r["obs_date"]: r["value"] for r in lower_rows}
        y3m_map = {r["obs_date"]: r["value"] for r in y3m_rows}
        y1y_map = {r["obs_date"]: r["value"] for r in y1y_rows}

        dates = sorted(set(y3m_map) | set(y1y_map))
        rows = []
        for d in dates:
            upper = upper_map.get(d)
            lower = lower_map.get(d)
            mid = None if upper is None or lower is None else round((upper + lower) / 2, 3)
            rows.append(
                {
                    "date": d,
                    "fed_funds_upper": upper,
                    "fed_funds_lower": lower,
                    "fed_funds_mid": mid,
                    "implied_3m": y3m_map.get(d),
                    "implied_1y": y1y_map.get(d),
                }
            )
        return rows
    finally:
        conn.close()


@app.get("/api/bonds/taylor-rule")
def bonds_taylor_rule():
    """泰勒法則(Taylor Rule)推算隱含政策利率，跟實際聯邦資金目標區間(上限/下限/中位數)比較。

        泰勒法則利率 = 中性名目利率 + 1.5*(π - π*) + 0.5*產出缺口

    π 為核心PCE物價指數年增率(Fed 最愛看的通膨指標)、π*=2% 為官方通膨目標(不變)，
    產出缺口 = (實質GDP - CBO估計的實質潛在GDP) / 實質潛在GDP * 100。

    「中性名目利率」(即經典公式裡的 r*+π*) 用 FOMC「經濟展望摘要(SEP)」對長期聯邦
    資金利率的中位數預測(FEDTARMDLR)，而不是教科書上固定的 2%+2%=4%——這是跟
    Atlanta Fed 官方 Taylor Rule Utility 對齊的關鍵：該工具預設用「時變」的均衡實質
    利率估計(Laubach-Williams / FOMC SEP 預測)，不是固定 2%。固定 r*=2% 版本算出來
    的隱含利率之所以會比 Atlanta Fed 官方數字高出一大截，主要就是這裡的差異；FRED
    沒有 Laubach-Williams 模型估計值的公開序列，但 FEDTARMDLR 是 Atlanta Fed 本身也
    支援的其中一種 r* 資料來源(FOMC participant projections)，兩者用同一套邏輯量測，
    可以直接對得上。SEP 每季發布一次，GDP 也是季頻，這條線大約一季變動一次。
    """
    conn = get_connection()
    try:
        gdp_rows = conn.execute(
            "SELECT obs_date, value FROM bond_series WHERE series_id = 'GDPC1' ORDER BY obs_date",
        ).fetchall()
        pot_rows = conn.execute(
            "SELECT obs_date, value FROM bond_series WHERE series_id = 'GDPPOT' ORDER BY obs_date",
        ).fetchall()
        pce_rows = conn.execute(
            "SELECT obs_date, value FROM bond_series WHERE series_id = 'PCEPILFE' ORDER BY obs_date",
        ).fetchall()

        pot_map = {r["obs_date"]: r["value"] for r in pot_rows}
        pce_map = {r["obs_date"]: r["value"] for r in pce_rows}
        pce_dates = sorted(pce_map)

        def pce_yoy(on_or_before: str) -> float | None:
            """核心PCE指數在 on_or_before(含) 之前最近一個月的年增率(跟12個月前同月比較)。"""
            candidates = [d for d in pce_dates if d <= on_or_before]
            if not candidates:
                return None
            d_now = candidates[-1]
            target_prior = (date.fromisoformat(d_now) - timedelta(days=365)).isoformat()
            prior_candidates = [d for d in pce_dates if d <= target_prior]
            if not prior_candidates:
                return None
            d_prior = prior_candidates[-1]
            v_now, v_prior = pce_map[d_now], pce_map[d_prior]
            if not v_prior:
                return None
            return (v_now / v_prior - 1) * 100

        pi_star = 2.0
        rows = []
        for g in gdp_rows:
            qd = g["obs_date"]
            gdp = g["value"]
            pot = pot_map.get(qd)
            infl = pce_yoy(qd)
            _, neutral_nominal_rate = _bond_nearest(conn, "FEDTARMDLR", qd)
            if pot is None or infl is None or neutral_nominal_rate is None:
                continue
            output_gap = (gdp - pot) / pot * 100
            taylor_rate = neutral_nominal_rate + 1.5 * (infl - pi_star) + 0.5 * output_gap

            _, upper = _bond_nearest(conn, "DFEDTARU", qd)
            _, lower = _bond_nearest(conn, "DFEDTARL", qd)
            mid = None if upper is None or lower is None else (upper + lower) / 2

            rows.append(
                {
                    "date": qd,
                    "taylor_rate": round(taylor_rate, 2),
                    "output_gap": round(output_gap, 2),
                    "core_pce_yoy": round(infl, 2),
                    "neutral_nominal_rate": neutral_nominal_rate,
                    "fed_funds_upper": upper,
                    "fed_funds_lower": lower,
                    "fed_funds_mid": round(mid, 3) if mid is not None else None,
                }
            )
        return rows
    finally:
        conn.close()


@app.get("/api/bonds/mpt-probability")
def bonds_mpt_probability():
    """Atlanta Fed「Market Probability Tracker」：CME 3個月期SOFR選擇權隱含的下一次
    政策利率決議「降息/升息」機率走勢。每個觀測日對應「最近一次到期窗口」的機率
    (近似「最近一次FOMC會議」)，資料處理邏輯見 app/fetch_mpt.py。
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT obs_date, reference_meeting, prob_cut, prob_hike FROM mpt_probability ORDER BY obs_date",
        ).fetchall()
        return [
            {
                "date": r["obs_date"],
                "reference_meeting": r["reference_meeting"],
                "prob_cut": r["prob_cut"],
                "prob_hike": r["prob_hike"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def _shift_months(d: date, delta: int) -> date:
    """d 往前/往後推 delta 個月，回傳當月 1 號。"""
    total = d.year * 12 + (d.month - 1) + delta
    year, month0 = divmod(total, 12)
    return date(year, month0 + 1, 1)


def _rates_from_index(rows: list) -> dict[str, dict]:
    """rows 是物價指數水準 (obs_date, value)，回傳 {obs_date: {"yoy":..,"mom":..}}。

    用「日期」(而不是陣列位置) 去找「12個月前」「1個月前」對應的值再算年增率/
    月增率——不能直接往前數固定幾格，因為資料可能缺月 (例如 2025年10月美國政府
    關門、BLS 當月沒發布 CPI 報告，FRED/BLS 的序列都會直接跳過那個月不给值)。
    如果用陣列位置固定往前推12/1格，缺月之後所有後續的年增率/月增率就會全部抓
    錯到差一個月的數字、整批系統性偏移，這是先前版本算出來的年增率跟官方公布
    數字對不起來的主因之一。
    """
    value_map = {r["obs_date"]: r["value"] for r in rows}
    out = {}
    for obs_date, value in value_map.items():
        d = date.fromisoformat(obs_date)
        prior_month_v = value_map.get(_shift_months(d, -1).isoformat())
        prior_year_v = value_map.get(_shift_months(d, -12).isoformat())
        yoy = round((value / prior_year_v - 1) * 100, 2) if prior_year_v else None
        mom = round((value / prior_month_v - 1) * 100, 2) if prior_month_v else None
        out[obs_date] = {"yoy": yoy, "mom": mom}
    return out


# CPI 相關項目資料源是 BLS 官方 API (app/fetch_cpi.py)，不是 FRED：
# item_key -> (中文標籤, 分類, 季調後序列代碼SA, 未季調序列代碼NSA)
# BLS 官方公布 CPI 慣例—年增率(YoY)用未季調(NSA)指數算、月增率(MoM)用季調後(SA)
# 指數算，兩種調整方式不能混用同一條序列，所以每個項目都存了 SA/NSA 兩條序列。
CPI_INFLATION_ITEMS: dict[str, tuple[str, str, str, str]] = {
    "CPI_ALL": ("CPI (綜合)", "CPI", "CUSR0000SA0", "CUUR0000SA0"),
    "CPI_CORE": ("核心CPI (排除食品與能源)", "CPI", "CUSR0000SA0L1E", "CUUR0000SA0L1E"),
    "CPI_FOOD": ("食品與飲料", "CPI_SUB", "CUSR0000SAF1", "CUUR0000SAF1"),
    "CPI_HOUSING": ("住房", "CPI_SUB", "CUSR0000SAH1", "CUUR0000SAH1"),
    "CPI_APPAREL": ("服飾", "CPI_SUB", "CUSR0000SAA", "CUUR0000SAA"),
    "CPI_TRANSPORT": ("交通運輸", "CPI_SUB", "CUSR0000SAT", "CUUR0000SAT"),
    "CPI_MEDICAL": ("醫療保健", "CPI_SUB", "CUSR0000SAM", "CUUR0000SAM"),
    "CPI_RECREATION": ("休閒娛樂", "CPI_SUB", "CUSR0000SAR", "CUUR0000SAR"),
    "CPI_EDUCATION": ("教育與通訊", "CPI_SUB", "CUSR0000SAE", "CUUR0000SAE"),
    "CPI_OTHER": ("其他商品與服務", "CPI_SUB", "CUSR0000SAG", "CUUR0000SAG"),
    "CPI_ENERGY": ("能源", "CPI_SUB", "CUSR0000SA0E", "CUUR0000SA0E"),
}

# PCE 維持用 FRED (BEA 沒有像 BLS 那樣的公開查詢API)：Fed 官方引用的「核心PCE年增率」
# 慣例是 YoY/MoM 都直接用季調後(SA)指數算，跟 CPI 的 NSA/SA 分工慣例不同，這裡不拆
# SA/NSA、單一序列直接算兩種增率。item_key -> (中文標籤, 分類, 序列代碼)
PCE_INFLATION_ITEMS: dict[str, tuple[str, str, str]] = {
    "PCE_ALL": ("PCE物價指數 (綜合)", "PCE", "PCEPI"),
    "PCE_CORE": ("核心PCE物價指數 (排除食品與能源)", "PCE", "PCEPILFE"),
}


@app.get("/api/bonds/inflation")
def bonds_inflation():
    """美國 CPI / 核心CPI / CPI主要分項 / PCE / 核心PCE 的年增率(YoY)與月增率(MoM)
    時間序列。CPI 相關的資料源是 BLS 官方 API (見 app/fetch_cpi.py)，年增率用未
    季調(NSA)序列、月增率用季調後(SA)序列，符合 BLS CPI 新聞稿的官方慣例；PCE
    維持用 FRED，YoY/MoM 都用季調後(SA)序列。兩者都是從物價指數水準自行計算
    (FRED/BLS 只給指數水準，不給轉換後的百分比)，見 _rates_from_index()。
    """
    conn = get_connection()

    def _query(series_id: str):
        return conn.execute(
            "SELECT obs_date, value FROM bond_series WHERE series_id = ? ORDER BY obs_date",
            (series_id,),
        ).fetchall()

    try:
        result = {}
        for key, (label, group, sa_id, nsa_id) in CPI_INFLATION_ITEMS.items():
            sa_rates = _rates_from_index(_query(sa_id))
            nsa_rates = _rates_from_index(_query(nsa_id))
            dates = sorted(set(sa_rates) | set(nsa_rates))
            series = [
                {
                    "date": d,
                    "yoy": nsa_rates.get(d, {}).get("yoy"),
                    "mom": sa_rates.get(d, {}).get("mom"),
                }
                for d in dates
            ]
            result[key] = {"label": label, "group": group, "series": series}

        for key, (label, group, sid) in PCE_INFLATION_ITEMS.items():
            rates = _rates_from_index(_query(sid))
            series = [{"date": d, **rates[d]} for d in sorted(rates)]
            result[key] = {"label": label, "group": group, "series": series}

        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 總經數據 (Macros)：PPI、非農就業(CES)、家戶調查(失業率/失業原因/失業週數/兼職)
# 資料源全部是 BLS 官方 API，見 app/fetch_macro.py。
# ---------------------------------------------------------------------------

PPI_INFLATION_ITEMS: dict[str, tuple[str, str, str, str]] = {
    "PPI_ALL": ("PPI Final Demand (綜合)", "PPI", "WPSFD4", "WPUFD4"),
    "PPI_CORE": ("核心PPI Final Demand (排除食品與能源)", "PPI", "WPSFD49104", "WPUFD49104"),
}

PAYROLL_ITEMS: dict[str, str] = {
    "CES0000000001": "非農就業總數",
    "CES1000000001": "採礦與伐木業",
    "CES2000000001": "營建業",
    "CES3000000001": "製造業",
    "CES4000000001": "批發零售運輸倉儲公用事業",
    "CES5000000001": "資訊業",
    "CES5500000001": "金融活動",
    "CES6000000001": "專業與商業服務",
    "CES6500000001": "教育與醫療服務",
    "CES7000000001": "休閒與旅宿業",
    "CES8000000001": "其他服務業",
    "CES9000000001": "政府部門",
}

HOUSEHOLD_RATE_ITEMS: dict[str, str] = {
    "LNS14000000": "失業率",
    "LNS11300000": "勞動力參與率",
    "LNS12300000": "就業人口比",
}

UNEMPLOY_REASON_ITEMS: dict[str, str] = {
    "LNS13023621": "遭資遣/解僱",
    "LNS13023705": "主動離職",
    "LNS13023557": "重返勞動市場",
    "LNS13023569": "初次尋職",
}

PARTTIME_ITEMS: dict[str, str] = {
    "LNS12032194": "兼職-經濟因素",
    "LNS12032196": "兼職-非經濟因素",
}


def _query_series(conn, series_id: str):
    return conn.execute(
        "SELECT obs_date, value FROM bond_series WHERE series_id = ? ORDER BY obs_date",
        (series_id,),
    ).fetchall()


def _level_changes(rows: list) -> dict[str, float]:
    """rows 是水準資料 (obs_date, value)，回傳 {obs_date: 較上月的絕對變化量}。
    用日期(而非陣列位置)找「上個月」，理由同 _rates_from_index()：資料可能缺月。
    """
    value_map = {r["obs_date"]: r["value"] for r in rows}
    out = {}
    for obs_date, value in value_map.items():
        prior = value_map.get(_shift_months(date.fromisoformat(obs_date), -1).isoformat())
        if prior is not None:
            out[obs_date] = round(value - prior, 1)
    return out


@app.get("/api/macro/ppi")
def macro_ppi():
    """美國 PPI Final Demand / 核心PPI 的年增率(YoY)與月增率(MoM)，計算方式跟
    /api/bonds/inflation 的 CPI 一樣：YoY 用未季調(NSA)序列、MoM 用季調後(SA)序列。
    """
    conn = get_connection()
    try:
        result = {}
        for key, (label, group, sa_id, nsa_id) in PPI_INFLATION_ITEMS.items():
            sa_rates = _rates_from_index(_query_series(conn, sa_id))
            nsa_rates = _rates_from_index(_query_series(conn, nsa_id))
            dates = sorted(set(sa_rates) | set(nsa_rates))
            series = [
                {"date": d, "yoy": nsa_rates.get(d, {}).get("yoy"), "mom": sa_rates.get(d, {}).get("mom")}
                for d in dates
            ]
            result[key] = {"label": label, "group": group, "series": series}
        return result
    finally:
        conn.close()


@app.get("/api/macro/payroll")
def macro_payroll():
    """非農就業總數 + 11 個主要產業別的「月增減人數」(千人)，用季調後水準逐月相減
    算出來(BLS 本身只給水準、不給月增減)。產業別加總理論上會等於總數(BLS 官方
    分類設計上互斥且完整)，實務上因為各序列各自獨立做季節調整，加總會有些微差異。
    """
    conn = get_connection()
    try:
        changes: dict[str, dict[str, float]] = {}
        for sid in PAYROLL_ITEMS:
            changes[sid] = _level_changes(_query_series(conn, sid))
        all_dates = sorted(set().union(*[set(c) for c in changes.values()]))
        return {
            "dates": all_dates,
            "items": [
                {
                    "series_id": sid,
                    "label": label,
                    "changes": [changes[sid].get(d) for d in all_dates],
                }
                for sid, label in PAYROLL_ITEMS.items()
            ],
        }
    finally:
        conn.close()


@app.get("/api/macro/household-rates")
def macro_household_rates():
    """失業率／勞動力參與率／就業人口比，三條線共用同一張時間序列圖。"""
    conn = get_connection()
    try:
        result = {}
        for sid, label in HOUSEHOLD_RATE_ITEMS.items():
            rows = _query_series(conn, sid)
            result[sid] = {"label": label, "series": [{"date": r["obs_date"], "value": r["value"]} for r in rows]}
        return result
    finally:
        conn.close()


@app.get("/api/macro/unemployment-reason")
def macro_unemployment_reason():
    """失業原因分類 (遭資遣/解僱、主動離職、重返勞動市場、初次尋職)，千人，已季調。"""
    conn = get_connection()
    try:
        result = {}
        for sid, label in UNEMPLOY_REASON_ITEMS.items():
            rows = _query_series(conn, sid)
            result[sid] = {"label": label, "series": [{"date": r["obs_date"], "value": r["value"]} for r in rows]}
        return result
    finally:
        conn.close()


@app.get("/api/macro/unemployment-duration")
def macro_unemployment_duration():
    """失業週數分類，千人，已季調。BLS 直接發布的是「<5週」「5-14週」「15週以上
    (累計)」「27週以上」，這裡把「15週以上」減掉「27週以上」，算出獨立不重疊的
    「15-26週」，湊成四個互斥區間方便疊圖。
    """
    conn = get_connection()
    try:
        under5 = {r["obs_date"]: r["value"] for r in _query_series(conn, "LNS13008396")}
        w5_14 = {r["obs_date"]: r["value"] for r in _query_series(conn, "LNS13008756")}
        w15_plus = {r["obs_date"]: r["value"] for r in _query_series(conn, "LNS13008516")}
        w27_plus = {r["obs_date"]: r["value"] for r in _query_series(conn, "LNS13008636")}

        dates = sorted(set(under5) | set(w5_14) | set(w15_plus) | set(w27_plus))
        w15_26 = {}
        for d in dates:
            if d in w15_plus and d in w27_plus:
                w15_26[d] = round(w15_plus[d] - w27_plus[d], 1)

        def series_of(m: dict) -> list[dict]:
            return [{"date": d, "value": m.get(d)} for d in dates]

        return {
            "LT5": {"label": "失業<5週", "series": series_of(under5)},
            "W5_14": {"label": "失業5-14週", "series": series_of(w5_14)},
            "W15_26": {"label": "失業15-26週", "series": series_of(w15_26)},
            "W27_PLUS": {"label": "失業27週以上", "series": series_of(w27_plus)},
        }
    finally:
        conn.close()


@app.get("/api/macro/parttime")
def macro_parttime():
    """兼職工作分類 (經濟因素 vs 非經濟因素)，千人，已季調。"""
    conn = get_connection()
    try:
        result = {}
        for sid, label in PARTTIME_ITEMS.items():
            rows = _query_series(conn, sid)
            result[sid] = {"label": label, "series": [{"date": r["obs_date"], "value": r["value"]} for r in rows]}
        return result
    finally:
        conn.close()


# Macros 頁面頂部統計卡片：CPI/核心CPI/PPI/核心PPI 用 (label, group, SA序列, NSA序列)，
# 非農就業總數/失業率/勞動力參與率用 (key, label, 序列代碼)。
RATE_STAT_ITEMS: list[tuple[str, tuple[str, str, str, str]]] = [
    ("CPI_ALL", CPI_INFLATION_ITEMS["CPI_ALL"]),
    ("CPI_CORE", CPI_INFLATION_ITEMS["CPI_CORE"]),
    ("PPI_ALL", PPI_INFLATION_ITEMS["PPI_ALL"]),
    ("PPI_CORE", PPI_INFLATION_ITEMS["PPI_CORE"]),
]

LEVEL_STAT_ITEMS: list[tuple[str, str, str, str]] = [
    ("PAYROLL_TOTAL", "非農就業總數", "CES0000000001", "千人"),
    ("UNEMPLOYMENT_RATE", "失業率", "LNS14000000", "%"),
    ("PARTICIPATION_RATE", "勞動力參與率", "LNS11300000", "%"),
]


@app.get("/api/macro/stats")
def macro_stats():
    """Macros 頁面頂部統計卡片：
    - CPI/核心CPI/PPI/核心PPI：最新一期的年增率(YoY)/月增率(MoM)，以及跟前一期
      (上個月自己的YoY/MoM) 相比的變化(百分點)。
    - 非農就業總數/失業率/勞動力參與率：最新值與前一期的值、變化量。
    """
    conn = get_connection()
    try:
        cards = []

        for key, (label, group, sa_id, nsa_id) in RATE_STAT_ITEMS:
            sa_rates = _rates_from_index(_query_series(conn, sa_id))
            nsa_rates = _rates_from_index(_query_series(conn, nsa_id))
            dates = sorted(set(sa_rates) | set(nsa_rates))
            combined = [
                {"date": d, "yoy": nsa_rates.get(d, {}).get("yoy"), "mom": sa_rates.get(d, {}).get("mom")}
                for d in dates
            ]
            valid = [r for r in combined if r["yoy"] is not None]
            if not valid:
                continue
            latest = valid[-1]
            prior = valid[-2] if len(valid) >= 2 else None
            yoy_delta = round(latest["yoy"] - prior["yoy"], 2) if prior and prior["yoy"] is not None else None
            mom_delta = (
                round(latest["mom"] - prior["mom"], 2)
                if prior and latest["mom"] is not None and prior["mom"] is not None
                else None
            )
            cards.append(
                {
                    "key": key,
                    "label": label,
                    "type": "rate",
                    "date": latest["date"],
                    "yoy": latest["yoy"],
                    "yoy_delta": yoy_delta,
                    "mom": latest["mom"],
                    "mom_delta": mom_delta,
                }
            )

        for key, label, sid, unit in LEVEL_STAT_ITEMS:
            rows = _query_series(conn, sid)
            if not rows:
                continue
            latest = rows[-1]
            prior = rows[-2] if len(rows) >= 2 else None
            delta = round(latest["value"] - prior["value"], 2) if prior else None
            cards.append(
                {
                    "key": key,
                    "label": label,
                    "type": "level",
                    "unit": unit,
                    "date": latest["obs_date"],
                    "value": latest["value"],
                    "prior_date": prior["obs_date"] if prior else None,
                    "prior_value": prior["value"] if prior else None,
                    "delta": delta,
                }
            )

        return cards
    finally:
        conn.close()
