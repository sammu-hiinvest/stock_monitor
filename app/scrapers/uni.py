"""統一投信 (ezmoney.com.tw) 申購買回清單 scraper。

API: POST https://www.ezmoney.com.tw/ETF/Transaction/GetPCF
Body: {"fundCode": "<內部代碼>", "date": "115/09/01" (民國年), "specificDate": false}
適用：00981A / 00403A / 00988A
"""

from datetime import date

from app.scrapers.common import make_session, parse_any_date, read_json, to_float, today_roc

BASE_URL = "https://www.ezmoney.com.tw/ETF/Transaction/GetPCF"

_ASSET_TYPE_MAP = {
    "ST": "STOCK",
    "GD": "FUTURES",
    "BD": "BOND",
    "ETF": "ETF",
}

_PCF_FIELD_MAP = {
    "NAV": "net_asset_value",
    "OUT_UNIT": "total_units",
    "DIFF_UNIT": "units_diff",
    "P_UNIT": "nav_per_unit",
    "NAV_PEOPLE": "beneficiaries_count",
}


def fetch(fund_code: str, on_date: date | None = None) -> dict:
    """on_date=None 取最新一期；指定日期(回補歷史用)時，該日期是「公告日」，回傳的是前一個
    交易日收盤的資料，資料實際日期以回應裡的 TranDate 為準。"""
    session = make_session()
    query_date = today_roc() if on_date is None else f"{on_date.year - 1911}/{on_date.month:02d}/{on_date.day:02d}"
    resp = session.post(
        BASE_URL,
        json={"fundCode": fund_code, "date": query_date, "specificDate": on_date is not None},
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "Referer": "https://www.ezmoney.com.tw/ETF/Transaction/PCF",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = read_json(resp)

    summary = {"beneficiaries_count": None}
    trade_date = None
    for row in data.get("pcf", []):
        code = row.get("PCFCode")
        if code in _PCF_FIELD_MAP:
            summary[_PCF_FIELD_MAP[code]] = to_float(row.get("Amount"))
        if trade_date is None:
            trade_date = parse_any_date(row.get("TranDate"))

    holdings = []
    for asset_class in data.get("asset") or []:
        asset_type = _ASSET_TYPE_MAP.get(asset_class.get("AssetCode"), "OTHER")
        for d in asset_class.get("Details") or []:
            holdings.append(
                {
                    "asset_type": asset_type,
                    "code": d.get("DetailCode", "").strip(),
                    "name": d.get("DetailName", "").strip(),
                    "shares": to_float(d.get("Share")),
                    "weight_pct": to_float(d.get("NavRate")),
                    "market_value": to_float(d.get("Amount")),
                }
            )

    return {
        "date": trade_date,
        "nav_per_unit": summary.get("nav_per_unit"),
        "total_units": summary.get("total_units"),
        "units_diff": summary.get("units_diff"),
        "net_asset_value": summary.get("net_asset_value"),
        "beneficiaries_count": summary.get("beneficiaries_count"),
        "holdings": holdings,
        "raw": data,
    }
