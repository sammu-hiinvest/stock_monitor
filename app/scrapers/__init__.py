from app.scrapers import uni, fuhwa, nomura, capital

DISPATCH = {
    "uni": uni.fetch,
    "fuhwa": fuhwa.fetch,
    "nomura": nomura.fetch,
    "capital": capital.fetch,
}


def fetch_for(issuer_code: str, fund_code: str) -> dict:
    """回傳統一格式：
    {
        "date": "YYYY-MM-DD",
        "nav_per_unit": float, "total_units": float, "units_diff": float,
        "net_asset_value": float, "beneficiaries_count": float | None,
        "holdings": [{"asset_type","code","name","shares","weight_pct","market_value"}, ...],
        "raw": <原始 JSON，供除錯/備份用>
    }
    """
    handler = DISPATCH.get(issuer_code)
    if handler is None:
        raise ValueError(f"未知的投信代碼: {issuer_code}")
    return handler(fund_code)
