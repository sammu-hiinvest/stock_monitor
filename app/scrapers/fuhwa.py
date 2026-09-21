"""復華投信 (fhtrust.com.tw) 申購買回清單 scraper。

API (GET, 免驗證):
  https://www.fhtrust.com.tw/api/ETFPcf?fundID=<代碼>&pcfDate=YYYY/MM/DD   -> 淨值/規模摘要
  https://www.fhtrust.com.tw/api/assets?fundID=<代碼>&qDate=YYYY/MM/DD    -> 持股明細
適用：00991A (fundID=ETF23)
"""

from datetime import date

from app.scrapers.common import make_session, parse_any_date, read_json, to_float, today_slash

PCF_URL = "https://www.fhtrust.com.tw/api/ETFPcf"
ASSETS_URL = "https://www.fhtrust.com.tw/api/assets"

_FTYPE_MAP = {
    "股票": "STOCK",
    "期貨": "FUTURES",
    "債券": "BOND",
    "ETF": "ETF",
}


def fetch(fund_id: str, on_date: date | None = None) -> dict:
    session = make_session()
    today = today_slash() if on_date is None else on_date.strftime("%Y/%m/%d")
    referer = "https://www.fhtrust.com.tw/ETF/trade_list"

    pcf_resp = session.get(
        PCF_URL, params={"fundID": fund_id, "pcfDate": today},
        headers={"Referer": referer}, timeout=20,
    )
    pcf_resp.raise_for_status()
    pcf_json = read_json(pcf_resp)
    pcf_row = (pcf_json.get("result") or [{}])[0]

    # /api/assets 要的是實際交易基準日 (前一個營業日)，不是查詢當天，
    # 從 PCF 回應的 actBuyValDate 取得該基準日再查一次。
    # 注意：pcf_row 的 "postDate" 是公告日(通常是查詢當天)，不是資料實際反映的
    # 交易基準日，不能拿來當作 holdings 的 date，否則會跟其他投信的資料日期定義對不上。
    assets_date = pcf_row.get("actBuyValDate") or pcf_row.get("actCashDiffDate") or today

    assets_resp = session.get(
        ASSETS_URL, params={"fundID": fund_id, "qDate": assets_date},
        headers={"Referer": referer}, timeout=20,
    )
    assets_resp.raise_for_status()
    assets_json = read_json(assets_resp)
    assets_root = (assets_json.get("result") or [{}])
    assets_root = assets_root[0] if isinstance(assets_root, list) else assets_root

    trade_date = parse_any_date(assets_root.get("dDate")) or parse_any_date(assets_date)

    holdings = []
    for d in assets_root.get("detail") or []:
        weight_raw = d.get("prate_addaccint")
        weight = None
        if isinstance(weight_raw, str):
            weight = to_float(weight_raw.replace("%", ""))
        holdings.append(
            {
                "asset_type": _FTYPE_MAP.get(d.get("ftype"), "OTHER"),
                "code": (d.get("stockid") or "").strip() or (d.get("stockname") or "").strip(),
                "name": (d.get("stockname") or "").strip(),
                "shares": to_float(d.get("qshare")),
                "weight_pct": weight,
                "market_value": to_float(d.get("mvalue")),
            }
        )

    return {
        "date": trade_date,
        "nav_per_unit": to_float(pcf_row.get("pnav")),
        "total_units": to_float(pcf_row.get("qIssue")),
        "units_diff": to_float(pcf_row.get("qDiff")),
        "net_asset_value": to_float(pcf_row.get("nav")),
        "beneficiaries_count": None,
        "holdings": holdings,
        "raw": {"pcf": pcf_json, "assets": assets_json},
    }
