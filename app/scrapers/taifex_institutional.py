"""期交所 (TAIFEX) 三大法人期貨交易報告 (區分各期貨契約/依日期) scraper。

API: POST https://www.taifex.com.tw/cht/3/futContractsDateDown
Body (form-urlencoded):
    queryStartDate=YYYY/MM/DD
    queryEndDate=YYYY/MM/DD
    commodityId=<TXF/MXF/TMF>

回應是 Big5(MS950) 編碼的 CSV，包含自營商/投信/外資及陸資三類法人的
多方/空方交易口數與契約金額、多方/空方未平倉口數與契約金額。

注意：這裡的 commodityId 代碼(TXF/MXF/TMF)跟「期貨每日交易行情下載」
(taifex_futures.py) 用的代碼(TX/MTX/TMF)是不同一套，尤其小型臺指這邊叫
MXF、那邊叫 MTX，容易搞混，兩個 scraper 各自獨立維護自己的代碼對照表。
"""

import csv
import io
from datetime import date

from app.scrapers.common import make_session

URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"

PRODUCTS = {
    "TX": "TXF",
    "MTX": "MXF",
    "TMF": "TMF",
}

_IDENTITY_MAP = {
    "自營商": "DEALER",
    "投信": "TRUST",
    "外資及陸資": "FOREIGN",
}


def _parse_num(raw: str) -> float | None:
    if raw is None:
        return None
    raw = raw.strip().replace(",", "")
    if raw in ("", "-"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_product_range(product_code: str, commodity_id: str, start: date, end: date) -> list[dict]:
    session = make_session()
    resp = session.post(
        URL,
        data={
            "queryStartDate": start.strftime("%Y/%m/%d"),
            "queryEndDate": end.strftime("%Y/%m/%d"),
            "commodityId": commodity_id,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.taifex.com.tw/cht/3/futContractsDateView",
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.content.decode("big5", errors="replace")

    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # 表頭

    rows = []
    for cols in reader:
        if len(cols) < 15:
            continue
        trade_date_raw = cols[0].strip()
        if not trade_date_raw or "/" not in trade_date_raw:
            continue
        identity = _IDENTITY_MAP.get(cols[2].strip())
        if identity is None:
            continue
        y, m, d = trade_date_raw.split("/")
        trade_date = f"{y}-{int(m):02d}-{int(d):02d}"

        rows.append(
            {
                "trade_date": trade_date,
                "product": product_code,
                "investor_type": identity,
                "long_volume": _parse_num(cols[3]),
                "long_value": _parse_num(cols[4]),
                "short_volume": _parse_num(cols[5]),
                "short_value": _parse_num(cols[6]),
                "net_volume": _parse_num(cols[7]),
                "net_value": _parse_num(cols[8]),
                "long_oi": _parse_num(cols[9]),
                "long_oi_value": _parse_num(cols[10]),
                "short_oi": _parse_num(cols[11]),
                "short_oi_value": _parse_num(cols[12]),
                "net_oi": _parse_num(cols[13]),
                "net_oi_value": _parse_num(cols[14]),
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
