"""期交所 (TAIFEX) 台指選擇權 (TXO) 每日交易行情下載 scraper。

API: POST https://www.taifex.com.tw/cht/3/dlOptDataDown
Body (form-urlencoded):
    down_type=1
    commodity_id=TXO    (固定 7 碼，後面補空白，跟官網下拉選單送出的值一致)
    commodity_id2=
    queryStartDate=YYYY/MM/DD
    queryEndDate=YYYY/MM/DD

回應是 Big5(MS950) 編碼的 CSV，附件檔名隨機、Content-Type 標成 text/html 但內容其實是 CSV。
官網註明查詢區間不得超過一個月。

CSV 每個 (交易日期, 到期月份, 履約價, 買賣權) 會有兩列：「一般」(日盤) 與「盤後」(夜盤)，
盤後那列的未沖銷契約數通常是 "-"（收盤未沖銷數以一般盤為準）。
"""

import csv
import io
from datetime import date

from app.scrapers.common import make_session

URL = "https://www.taifex.com.tw/cht/3/dlOptDataDown"
COMMODITY_ID = "TXO    "

_SESSION_MAP = {"一般": "REGULAR", "盤後": "AFTERHOURS"}
_TYPE_MAP = {"買權": "C", "賣權": "P"}


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


def fetch_range(start: date, end: date) -> list[dict]:
    """下載 start~end (含) 區間內所有 TXO 每日行情列，回傳攤平後的 list，尚未依日期分組。"""
    session_ = make_session()
    body = {
        "down_type": "1",
        "commodity_id": COMMODITY_ID,
        "commodity_id2": "",
        "queryStartDate": start.strftime("%Y/%m/%d"),
        "queryEndDate": end.strftime("%Y/%m/%d"),
    }
    resp = session_.post(
        URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.taifex.com.tw/cht/3/optDailyMarketView",
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.content.decode("big5", errors="replace")

    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # 表頭

    rows = []
    for cols in reader:
        if len(cols) < 21:
            continue
        trade_date_raw = cols[0].strip()
        if not trade_date_raw or "/" not in trade_date_raw:
            continue
        y, m, d = trade_date_raw.split("/")
        trade_date = f"{y}-{int(m):02d}-{int(d):02d}"

        expiry_raw = cols[20].strip()
        expiry_date = None
        if len(expiry_raw) == 8 and expiry_raw.isdigit():
            expiry_date = f"{expiry_raw[0:4]}-{expiry_raw[4:6]}-{expiry_raw[6:8]}"

        rows.append(
            {
                "trade_date": trade_date,
                "contract_month": cols[2].strip(),
                "strike_price": _parse_num(cols[3]),
                "option_type": _TYPE_MAP.get(cols[4].strip(), cols[4].strip()),
                "session": _SESSION_MAP.get(cols[17].strip(), cols[17].strip()),
                "open_price": _parse_num(cols[5]),
                "high_price": _parse_num(cols[6]),
                "low_price": _parse_num(cols[7]),
                "close_price": _parse_num(cols[8]),
                "volume": _parse_num(cols[9]) or 0,
                "settlement_price": _parse_num(cols[10]),
                "open_interest": _parse_num(cols[11]),
                "change_price": _parse_num(cols[18]),
                "change_pct": _parse_num(cols[19]),
                "expiry_date": expiry_date,
            }
        )
    return rows


def group_by_date(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["trade_date"], []).append(r)
    return grouped
