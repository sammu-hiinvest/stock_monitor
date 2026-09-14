"""個股收盤價擷取：先查上市(TWSE)，查不到再查上櫃(TPEx)。

用於「個股跨ETF比較」頁的價格走勢圖。兩個來源都是官方公開、免登入的每日成交資訊，
以「西元年月」為單位查詢(一次拿一整個月)：
  上市 TWSE : https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=YYYYMMDD&stockNo=CODE
  上櫃 TPEx : https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code=CODE&date=YYYY/MM/DD&response=json

非台股上市櫃的代碼 (例如基金持有的海外股票) 兩邊都查不到，視為「無資料」，
呼叫端會記錄到 stock_price_fetch_log 避免重複嘗試。
"""

import re
from datetime import date, datetime, timedelta

import requests

from app.config import HTTP_HEADERS
from app.db import get_fetched_year_months, mark_year_month_fetched, upsert_stock_prices
from app.scrapers.common import read_json, to_float

TWSE_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"

_ROC_DATE_RE = re.compile(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$")


def _roc_to_iso(roc_date: str) -> str | None:
    m = _ROC_DATE_RE.match(roc_date.strip())
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return date(y + 1911, mo, d).isoformat()
    except ValueError:
        return None


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HTTP_HEADERS)
    return s


def fetch_twse_month(session: requests.Session, code: str, year: int, month: int) -> list[dict] | None:
    query_date = f"{year:04d}{month:02d}01"
    try:
        resp = session.get(
            TWSE_URL,
            params={"response": "json", "date": query_date, "stockNo": code},
            headers={"Referer": "https://www.twse.com.tw/zh/trading/historical/stock-day.html"},
            timeout=20,
        )
        resp.raise_for_status()
        data = read_json(resp)
    except Exception:
        return None

    if data.get("stat") != "OK":
        return None

    rows = []
    for cols in data.get("data", []):
        iso_date = _roc_to_iso(cols[0])
        close = to_float(cols[6])
        if iso_date and close is not None:
            rows.append({"date": iso_date, "close": close, "source": "TWSE"})
    return rows


def fetch_tpex_month(session: requests.Session, code: str, year: int, month: int) -> list[dict] | None:
    query_date = f"{year:04d}/{month:02d}/01"
    try:
        resp = session.get(
            TPEX_URL,
            params={"code": code, "date": query_date, "id": "", "response": "json"},
            headers={"Referer": "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/stock-pricing.html"},
            timeout=20,
        )
        resp.raise_for_status()
        data = read_json(resp)
    except Exception:
        return None

    if data.get("stat") != "ok":
        return None
    tables = data.get("tables") or []
    if not tables:
        return None

    rows = []
    for cols in tables[0].get("data", []):
        iso_date = _roc_to_iso(cols[0])
        close = to_float(cols[6])
        if iso_date and close is not None:
            rows.append({"date": iso_date, "close": close, "source": "TPEX"})
    return rows


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    months = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        months.append((cur.year, cur.month))
        cur = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return months


def ensure_price_history(conn, code: str, start_date: str, end_date: str) -> None:
    """確保 stock_prices 表已經涵蓋 [start_date, end_date] 這段期間的資料
    (查得到的月份就存、查不到的月份就記錄下來不再重查)。
    """
    start = date.fromisoformat(start_date)
    end = min(date.fromisoformat(end_date), date.today())
    if start > end:
        return

    current_ym = f"{date.today().year:04d}-{date.today().month:02d}"
    already = get_fetched_year_months(conn, code)
    # 當月資料可能還在持續增加，每次都重查；更早的月份資料已定型，查過就跳過。
    needed = [
        (y, m)
        for (y, m) in _months_between(start, end)
        if f"{y:04d}-{m:02d}" == current_ym or f"{y:04d}-{m:02d}" not in already
    ]
    if not needed:
        return

    session = _make_session()
    fetched_at = datetime.now().isoformat(timespec="seconds")

    for y, m in needed:
        ym = f"{y:04d}-{m:02d}"
        rows = fetch_twse_month(session, code, y, m)
        if not rows:
            rows = fetch_tpex_month(session, code, y, m)

        if rows:
            upsert_stock_prices(conn, code, rows)
            mark_year_month_fetched(conn, code, ym, True, fetched_at)
        else:
            mark_year_month_fetched(conn, code, ym, False, fetched_at)
