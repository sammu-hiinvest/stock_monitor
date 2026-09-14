"""債市資料抓取主流程：美債殖利率曲線、利差、公司債利差、5Y5Y遠期通膨預期。
來源全部是 FRED 公開 CSV (見 app/scrapers/fred.py)，不需要 API 金鑰。
"""

import logging
from datetime import date, datetime, timedelta

from app.db import init_db, session, upsert_bond_series
from app.scrapers.fred import fetch_series

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fetch_bonds")

# FRED 序列代碼 -> (中文標籤, 分類, 排序用的年期月數)
# YIELD_CURVE：美債殖利率曲線各年期；SPREAD：公債利差；CREDIT：公司債利差；FORWARD：遠期通膨預期
BOND_SERIES: dict[str, tuple[str, str, int]] = {
    "DGS1MO": ("1M", "YIELD_CURVE", 1),
    "DGS3MO": ("3M", "YIELD_CURVE", 3),
    "DGS6MO": ("6M", "YIELD_CURVE", 6),
    "DGS1": ("1Y", "YIELD_CURVE", 12),
    "DGS2": ("2Y", "YIELD_CURVE", 24),
    "DGS3": ("3Y", "YIELD_CURVE", 36),
    "DGS5": ("5Y", "YIELD_CURVE", 60),
    "DGS7": ("7Y", "YIELD_CURVE", 84),
    "DGS10": ("10Y", "YIELD_CURVE", 120),
    "DGS20": ("20Y", "YIELD_CURVE", 240),
    "DGS30": ("30Y", "YIELD_CURVE", 360),
    "T10Y2Y": ("2Y10Y 利差", "SPREAD", 0),
    "T10Y3M": ("3M10Y 利差", "SPREAD", 0),
    "BAMLC0A4CBBB": ("BBB 投等公司債利差", "CREDIT", 0),
    "BAMLH0A0HYM2": ("高收益公司債利差", "CREDIT", 0),
    "T5YIFR": ("5Y5Y 遠期通膨預期", "FORWARD", 0),
    "T5YIE": ("5Y 損益兩平通膨率", "FORWARD", 0),
    # 聯邦資金目標區間（實際政策利率）+ 泰勒法則(Taylor Rule)計算所需的輸入資料。
    # POLICY_RATE / TAYLOR_INPUT 這兩類不進頂部關鍵指標卡片，只給「泰勒法則 vs 實際政策利率」
    # 這張專屬圖表用，見 app/main.py 的 /api/bonds/taylor-rule。
    "DFEDTARU": ("聯邦資金目標區間上限", "POLICY_RATE", 0),
    "DFEDTARL": ("聯邦資金目標區間下限", "POLICY_RATE", 0),
    "GDPC1": ("實質GDP", "TAYLOR_INPUT", 0),
    "GDPPOT": ("實質潛在GDP(CBO估計)", "TAYLOR_INPUT", 0),
    "PCEPILFE": ("核心PCE物價指數", "TAYLOR_INPUT", 0),
    # FOMC「經濟展望摘要(SEP)」對長期聯邦資金利率的中位數估計，拿來當「名目中性利率」
    # (r* + 通膨目標)，取代固定 2%+2%=4% 的教科書假設 —— 這是跟 Atlanta Fed Taylor Rule
    # Utility 對得上的關鍵：他們預設用「時變」的均衡實質利率估計(Laubach-Williams /
    # FOMC SEP 預測)，不是固定 2%，這也是為什麼固定 r*=2% 版本算出來的隱含利率會比
    # Atlanta Fed 官方工具高出一大截。
    "FEDTARMDLR": ("FOMC長期聯邦資金利率中位數預測(中性利率估計)", "TAYLOR_INPUT", 0),
    # 美國PCE通膨數據：PCE / 核心PCE 物價指數水準 (CPI 相關的改用 BLS 官方 API 當
    # 資料源，見 app/fetch_cpi.py / app/scrapers/bls.py，不再用 FRED 轉錄版本)。
    # FRED 這個序列本身只給「指數水準」，年增率(YoY)/月增率(MoM) 要自己從水準算，
    # 見 app/main.py 的 _rates_from_index() 與 /api/bonds/inflation。PCE_INDEX 這類
    # 不進頂部關鍵指標卡片(指數水準本身沒有意義，只用來算增率)。
    "PCEPI": ("PCE物價指數 (綜合)", "PCE_INDEX", 0),
}

DEFAULT_BACKFILL_DAYS = 730  # 預設回溯 2 年，足夠算 YTD 及較長的走勢圖

# 泰勒法則要算「年增率」(核心PCE YoY)跟產出缺口、通膨頁面要算PCE的YoY/MoM，
# 圖表要在 start~end 整段區間內每一點都算得出來，所以這些序列額外多抓 1 年當計算
# 基期，不然回溯區間最前面約 1 年會因為找不到「12個月前」的資料而算不出 YoY，
# 導致圖表前段是空的。
EXTRA_LOOKBACK_SERIES = {"GDPC1", "GDPPOT", "PCEPILFE", "PCEPI"}
EXTRA_LOOKBACK_DAYS = 365


def _store(series_id: str, rows: list[dict]) -> int:
    fetched_at = datetime.now().isoformat(timespec="seconds")
    for r in rows:
        r["fetched_at"] = fetched_at
    with session() as conn:
        upsert_bond_series(conn, series_id, rows)
    return len(rows)


def _fetch_all(start: date, end: date) -> dict:
    result = {}
    for series_id in BOND_SERIES:
        try:
            fetch_start = start - timedelta(days=EXTRA_LOOKBACK_DAYS) if series_id in EXTRA_LOOKBACK_SERIES else start
            rows = fetch_series(series_id, fetch_start, end)
            result[series_id] = _store(series_id, rows)
        except Exception as exc:  # noqa: BLE001 - 單一序列失敗不影響其他序列
            log.exception("%s 抓取失敗: %s", series_id, exc)
            result[series_id] = f"error: {exc}"
    return result


def backfill(start: date, end: date) -> dict:
    """一次補齊 start~end (含) 區間的所有債市序列。"""
    init_db()
    log.info("回溯下載債市資料 %s ~ %s ...", start, end)
    result = _fetch_all(start, end)
    log.info("債市資料回溯完成：%s", result)
    return result


def run_latest(lookback_days: int = 10) -> dict:
    """排程用：抓最近 lookback_days 天並覆寫，漏跑也能自動補齊。"""
    init_db()
    end = date.today()
    start = end - timedelta(days=lookback_days)
    log.info("抓取債市最新資料 %s ~ %s ...", start, end)
    result = _fetch_all(start, end)
    log.info("債市資料更新完成：%s", result)
    return result


if __name__ == "__main__":
    import json

    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
