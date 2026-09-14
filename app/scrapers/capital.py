"""群益投信 (capitalfund.com.tw) 申購買回清單 scraper。

API: POST https://www.capitalfund.com.tw/CFWeb/api/etf/buyback
Body: {"fundId": "<內部代碼>", "date": null}   (date 給 null 即取得最新一期)
適用：00982A (fundId=399) / 00992A (fundId=500)
"""

from app.scrapers.common import make_session, parse_any_date, read_json, to_float

URL = "https://www.capitalfund.com.tw/CFWeb/api/etf/buyback"


def fetch(fund_id: str) -> dict:
    session = make_session()
    resp = session.post(
        URL,
        json={"fundId": fund_id, "date": None},
        headers={
            "Content-Type": "application/json",
            "Referer": "https://www.capitalfund.com.tw/ETF_Area/Pcf",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = read_json(resp)
    payload = data.get("data") or {}
    pcf = payload.get("pcf") or {}

    trade_date = parse_any_date(pcf.get("date2"))

    holdings = []
    for row in payload.get("stocks", []):
        holdings.append(
            {
                "asset_type": "STOCK",
                "code": (row.get("stocNo") or "").strip(),
                "name": (row.get("stocName") or "").strip(),
                "shares": to_float(row.get("share")),
                "weight_pct": to_float(row.get("weight")),
                "market_value": None,
            }
        )

    return {
        "date": trade_date,
        "nav_per_unit": to_float(pcf.get("pUnit")),
        "total_units": to_float(pcf.get("totUnit")),
        "units_diff": to_float(pcf.get("disUnit")),
        "net_asset_value": to_float(pcf.get("nav")),
        "beneficiaries_count": to_float(pcf.get("numberPeople")),
        "holdings": holdings,
        "raw": data,
    }
