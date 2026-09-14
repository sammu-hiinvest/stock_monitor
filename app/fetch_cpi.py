"""美國 CPI 資料抓取主流程，資料源是 BLS (美國勞工統計局) 官方 API，不是 FRED。

換源原因見 app/scrapers/bls.py 開頭說明：BLS 官方公布 CPI 年增率(YoY)用未季調
(NSA)指數、月增率(MoM)用季調後(SA)指數，兩種都要各自獨立抓、不能共用同一條序列。

存進跟其他債市資料共用的 bond_series 表(series_id/obs_date/value 是通用結構，
BLS 的序列代碼字串跟 FRED 的不會撞名)，這樣 /api/bonds/inflation 可以用同一套
查詢邏輯處理。
"""

import logging
from datetime import date, datetime

from app.db import init_db, session, upsert_bond_series
from app.scrapers.bls import fetch_series

log = logging.getLogger("fetch_cpi")

# BLS 序列代碼 -> (中文標籤, 分類, SA或NSA)
# CPI：綜合、核心(排除食品與能源)；CPI_SUB：BLS 八大分類 + 能源。
# 每個項目都要 SA 跟 NSA 各抓一份：YoY 算在 NSA 序列上、MoM 算在 SA 序列上
# (見 app/main.py 的 /api/bonds/inflation)。
CPI_SERIES: dict[str, tuple[str, str, str]] = {
    "CUSR0000SA0": ("CPI (綜合)", "CPI", "SA"),
    "CUUR0000SA0": ("CPI (綜合)", "CPI", "NSA"),
    "CUSR0000SA0L1E": ("核心CPI (排除食品與能源)", "CPI", "SA"),
    "CUUR0000SA0L1E": ("核心CPI (排除食品與能源)", "CPI", "NSA"),
    "CUSR0000SAF1": ("食品與飲料", "CPI_SUB", "SA"),
    "CUUR0000SAF1": ("食品與飲料", "CPI_SUB", "NSA"),
    "CUSR0000SAH1": ("住房", "CPI_SUB", "SA"),
    "CUUR0000SAH1": ("住房", "CPI_SUB", "NSA"),
    "CUSR0000SAA": ("服飾", "CPI_SUB", "SA"),
    "CUUR0000SAA": ("服飾", "CPI_SUB", "NSA"),
    "CUSR0000SAT": ("交通運輸", "CPI_SUB", "SA"),
    "CUUR0000SAT": ("交通運輸", "CPI_SUB", "NSA"),
    "CUSR0000SAM": ("醫療保健", "CPI_SUB", "SA"),
    "CUUR0000SAM": ("醫療保健", "CPI_SUB", "NSA"),
    "CUSR0000SAR": ("休閒娛樂", "CPI_SUB", "SA"),
    "CUUR0000SAR": ("休閒娛樂", "CPI_SUB", "NSA"),
    "CUSR0000SAE": ("教育與通訊", "CPI_SUB", "SA"),
    "CUUR0000SAE": ("教育與通訊", "CPI_SUB", "NSA"),
    "CUSR0000SAG": ("其他商品與服務", "CPI_SUB", "SA"),
    "CUUR0000SAG": ("其他商品與服務", "CPI_SUB", "NSA"),
    "CUSR0000SA0E": ("能源", "CPI_SUB", "SA"),
    "CUUR0000SA0E": ("能源", "CPI_SUB", "NSA"),
}

# 抓 4 年：YoY 要多 1 年當基期，圖表希望呈現約 2~3 年走勢。BLS 未註冊額度上限是
# 每次查詢最多 10 年，4 年遠在額度內。
DEFAULT_BACKFILL_YEARS = 4


def _store_all(series_map: dict[str, list[dict]]) -> dict:
    fetched_at = datetime.now().isoformat(timespec="seconds")
    result = {}
    with session() as conn:
        for sid, rows in series_map.items():
            for r in rows:
                r["fetched_at"] = fetched_at
            upsert_bond_series(conn, sid, rows)
            result[sid] = len(rows)
    return result


def backfill(start_year: int, end_year: int) -> dict:
    """一次補齊 start_year~end_year(含) 的所有 BLS CPI 序列。"""
    init_db()
    log.info("回溯下載 BLS CPI 資料 %s ~ %s ...", start_year, end_year)
    series_map = fetch_series(list(CPI_SERIES.keys()), start_year, end_year)
    result = _store_all(series_map)
    log.info("BLS CPI 資料回溯完成：%s", result)
    return result


def run_latest() -> dict:
    """排程/手動用：BLS API 本身就是整段年份查詢、沒有增量端點，所以每次都直接
    重抓 DEFAULT_BACKFILL_YEARS 年份區間並覆寫，最省事、也最不會漏補到修訂值。
    """
    init_db()
    end_year = date.today().year
    start_year = end_year - DEFAULT_BACKFILL_YEARS
    log.info("抓取 BLS CPI 最新資料 %s ~ %s ...", start_year, end_year)
    series_map = fetch_series(list(CPI_SERIES.keys()), start_year, end_year)
    result = _store_all(series_map)
    log.info("BLS CPI 資料更新完成：%s", result)
    return result


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
