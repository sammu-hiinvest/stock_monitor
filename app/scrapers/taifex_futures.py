"""期交所 (TAIFEX) 期貨每日交易行情下載 scraper。

API: POST https://www.taifex.com.tw/cht/3/futDataDown
Body (form-urlencoded):
    down_type=1
    commodity_id=<TX/MTX/TMF，固定 7 碼後面補空白>
    commodity_id2=
    queryStartDate=YYYY/MM/DD
    queryEndDate=YYYY/MM/DD

回應是 Big5(MS950) 編碼的 CSV。每個 (交易日期, 契約, 到期月份) 會有「一般」(日盤)
與「盤後」(夜盤) 兩列，未沖銷契約數以「一般」盤收盤後公布的數字為準。
"""

import csv
import io
from datetime import date

from app.scrapers.common import make_session

URL = "https://www.taifex.com.tw/cht/3/futDataDown"

# 這裡的 commodity_id 是「期貨每日交易行情下載」頁面自己的代碼，
# 跟三大法人報告(taifex_institutional.py)用的代碼不是同一套，兩邊代碼不要混用。
PRODUCTS = {
    "TX": "TX     ",
    "MTX": "MTX    ",
    "TMF": "TMF    ",
}

_SESSION_MAP = {"一般": "REGULAR", "盤後": "AFTERHOURS"}


def _parse_num(raw: str) -> float | None:
    if raw is None:
        return None
    raw = raw.strip().replace(",", "")
    if raw in ("", "-"):
        return None
    if raw.endswith("%"):
        raw = raw[:-1]
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_product_range(product_code: str, commodity_id: str, start: date, end: date) -> list[dict]:
    session = make_session()
    resp = session.post(
        URL,
        data={
            "down_type": "1",
            "commodity_id": commodity_id,
            "commodity_id2": "",
            "queryStartDate": start.strftime("%Y/%m/%d"),
            "queryEndDate": end.strftime("%Y/%m/%d"),
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.taifex.com.tw/cht/3/futDailyMarketView",
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.content.decode("big5", errors="replace")

    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # 表頭

    rows = []
    for cols in reader:
        if len(cols) < 18:
            continue
        trade_date_raw = cols[0].strip()
        if not trade_date_raw or "/" not in trade_date_raw:
            continue
        y, m, d = trade_date_raw.split("/")
        trade_date = f"{y}-{int(m):02d}-{int(d):02d}"

        rows.append(
            {
                "trade_date": trade_date,
                "product": product_code,
                "contract_month": cols[2].strip(),
                "session": _SESSION_MAP.get(cols[17].strip(), cols[17].strip()),
                "open_price": _parse_num(cols[3]),
                "high_price": _parse_num(cols[4]),
                "low_price": _parse_num(cols[5]),
                "close_price": _parse_num(cols[6]),
                "change_price": _parse_num(cols[7]),
                "change_pct": _parse_num(cols[8]),
                "volume": _parse_num(cols[9]) or 0,
                "settlement_price": _parse_num(cols[10]),
                "open_interest": _parse_num(cols[11]),
            }
        )
    return rows


def fetch_all_products_range(start: date, end: date) -> list[dict]:
    rows: list[dict] = []
    for product_code, commodity_id in PRODUCTS.items():
        rows.extend(fetch_product_range(product_code, commodity_id, start, end))
    return rows


def group_by_date(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["trade_date"], []).append(r)
    return grouped
