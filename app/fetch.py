"""每日抓取主流程：對 WATCHLIST 逐一呼叫對應投信的 scraper，寫入 SQLite，
並把原始 JSON 備份到 data/raw/ 方便日後除錯或改寫 parser。
"""

import json
import logging
import os
from datetime import datetime

from app.config import RAW_DIR, WATCHLIST
from app.db import init_db, replace_holdings, session, upsert_fund, upsert_summary
from app.scrapers import fetch_for

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetch")


def _save_raw(ticker: str, date: str | None, raw: dict) -> None:
    tag = date or "unknown-date"
    out_dir = os.path.join(RAW_DIR, ticker)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2, default=str)


def run_all() -> dict:
    """回傳 {ticker: "ok"/"error: ..."} 的執行結果摘要。"""
    init_db()
    results: dict[str, str] = {}
    fetched_at = datetime.now().isoformat(timespec="seconds")

    for etf in WATCHLIST:
        try:
            log.info("抓取 %s (%s) ...", etf.ticker, etf.name)
            result = fetch_for(etf.issuer_code, etf.fund_code)

            if not result.get("date"):
                raise ValueError("回應中找不到有效的資料日期 (date)")
            if result.get("nav_per_unit") is None:
                # 非交易日(例如週末)查詢部分投信網站，可能回傳一個「今天」的空殼回應
                # (無淨值、無持股)，若照單全收會把假日寫成一筆壞資料，之後日期序列會出現斷點或誤導。
                raise ValueError("回應中沒有有效的淨值資料，可能是非交易日或來源網站當下無資料")
            if not result.get("holdings"):
                log.warning("%s 這次抓取沒有任何持股明細，仍會寫入淨值摘要", etf.ticker)

            with session() as conn:
                upsert_fund(conn, etf.ticker, etf.name, etf.issuer)
                upsert_summary(
                    conn,
                    {
                        "ticker": etf.ticker,
                        "date": result["date"],
                        "nav_per_unit": result.get("nav_per_unit"),
                        "total_units": result.get("total_units"),
                        "units_diff": result.get("units_diff"),
                        "net_asset_value": result.get("net_asset_value"),
                        "beneficiaries_count": result.get("beneficiaries_count"),
                        "fetched_at": fetched_at,
                    },
                )
                replace_holdings(conn, etf.ticker, result["date"], result.get("holdings", []))

            _save_raw(etf.ticker, result["date"], result.get("raw", {}))
            log.info(
                "%s OK - 資料日期 %s，持股 %d 檔",
                etf.ticker, result["date"], len(result.get("holdings", [])),
            )
            results[etf.ticker] = "ok"

        except Exception as exc:  # noqa: BLE001 - 個別基金失敗不影響其他基金
            log.exception("%s 抓取失敗: %s", etf.ticker, exc)
            results[etf.ticker] = f"error: {exc}"

    return results


if __name__ == "__main__":
    summary = run_all()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
