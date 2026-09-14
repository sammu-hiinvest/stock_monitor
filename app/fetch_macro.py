"""美國總經數據抓取主流程：PPI(生產者物價指數)、非農就業(CES企業調查)、
家戶調查(失業率/勞參率/就業人口比、失業原因、失業週數、兼職工作)。

資料源全部是 BLS(美國勞工統計局)官方 API v2，不需要金鑰，見 app/scrapers/bls.py。
存進跟 CPI 共用的 bond_series 表(series_id/obs_date/value 是通用結構)。
"""

import logging
from datetime import date, datetime

from app.db import init_db, session, upsert_bond_series
from app.scrapers.bls import fetch_series

log = logging.getLogger("fetch_macro")

# BLS 序列代碼 -> (中文標籤, 分類, SA或NSA)
#
# PPI：跟 CPI 一樣的官方慣例，年增率(YoY)用未季調(NSA)序列、月增率(MoM)用季調後
# (SA)序列，所以 SA/NSA 各抓一份。
#
# 非農就業(CES)、家戶調查(LNS)：前綴本身就已經是季調後數字(CES=季調/CEU=未季調、
# LNS=季調/LNU=未季調)，本專案一律只用官方慣例引用的 SA 版本，不切 NSA。
MACRO_SERIES: dict[str, tuple[str, str, str]] = {
    # --- PPI (Final Demand) ---
    "WPSFD4": ("PPI Final Demand (綜合)", "PPI", "SA"),
    "WPUFD4": ("PPI Final Demand (綜合)", "PPI", "NSA"),
    "WPSFD49104": ("核心PPI Final Demand (排除食品與能源)", "PPI", "SA"),
    "WPUFD49104": ("核心PPI Final Demand (排除食品與能源)", "PPI", "NSA"),
    # --- 非農就業 (CES，就業人數水準，千人，已季調) ---
    "CES0000000001": ("非農就業總數", "PAYROLL_TOTAL", "SA"),
    "CES1000000001": ("採礦與伐木業", "PAYROLL_SUB", "SA"),
    "CES2000000001": ("營建業", "PAYROLL_SUB", "SA"),
    "CES3000000001": ("製造業", "PAYROLL_SUB", "SA"),
    "CES4000000001": ("批發零售運輸倉儲公用事業", "PAYROLL_SUB", "SA"),
    "CES5000000001": ("資訊業", "PAYROLL_SUB", "SA"),
    "CES5500000001": ("金融活動", "PAYROLL_SUB", "SA"),
    "CES6000000001": ("專業與商業服務", "PAYROLL_SUB", "SA"),
    "CES6500000001": ("教育與醫療服務", "PAYROLL_SUB", "SA"),
    "CES7000000001": ("休閒與旅宿業", "PAYROLL_SUB", "SA"),
    "CES8000000001": ("其他服務業", "PAYROLL_SUB", "SA"),
    "CES9000000001": ("政府部門", "PAYROLL_SUB", "SA"),
    # --- 家戶調查：失業率/勞參率/就業人口比 (百分比，已季調) ---
    "LNS14000000": ("失業率", "HOUSEHOLD_RATE", "SA"),
    "LNS11300000": ("勞動力參與率", "HOUSEHOLD_RATE", "SA"),
    "LNS12300000": ("就業人口比", "HOUSEHOLD_RATE", "SA"),
    # --- 失業原因 (千人，已季調)：遭資遣/解僱、主動離職、重返勞動市場、初次尋職 ---
    "LNS13023621": ("遭資遣/解僱", "UNEMPLOY_REASON", "SA"),
    "LNS13023705": ("主動離職", "UNEMPLOY_REASON", "SA"),
    "LNS13023557": ("重返勞動市場", "UNEMPLOY_REASON", "SA"),
    "LNS13023569": ("初次尋職", "UNEMPLOY_REASON", "SA"),
    # --- 失業週數 (千人，已季調)：15週以上/27週以上是累計數字，"15-26週"在
    # app/main.py 端用兩者相減算出來(BLS沒有直接發布這個區間的獨立序列)。
    "LNS13008396": ("失業<5週", "UNEMPLOY_DURATION", "SA"),
    "LNS13008756": ("失業5-14週", "UNEMPLOY_DURATION", "SA"),
    "LNS13008516": ("失業15週以上(累計)", "UNEMPLOY_DURATION", "SA"),
    "LNS13008636": ("失業27週以上", "UNEMPLOY_DURATION", "SA"),
    # --- 兼職工作 (千人，已季調)：經濟因素(想找全職但只找得到兼職) vs 非經濟因素 ---
    "LNS12032194": ("兼職-經濟因素", "PARTTIME", "SA"),
    "LNS12032196": ("兼職-非經濟因素", "PARTTIME", "SA"),
}

# 抓 4 年：非農就業/PPI YoY 都要多 1 年當基期，圖表希望呈現約 2~3 年走勢。
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
    """一次補齊 start_year~end_year(含) 的所有 BLS 總經序列。"""
    init_db()
    log.info("回溯下載 BLS 總經資料 %s ~ %s ...", start_year, end_year)
    series_map = fetch_series(list(MACRO_SERIES.keys()), start_year, end_year)
    result = _store_all(series_map)
    log.info("BLS 總經資料回溯完成：%s", result)
    return result


def run_latest() -> dict:
    """排程/手動用：BLS API 本身就是整段年份查詢、沒有增量端點，所以每次都直接
    重抓 DEFAULT_BACKFILL_YEARS 年份區間並覆寫，最省事、也最不會漏補到修訂值。
    """
    init_db()
    end_year = date.today().year
    start_year = end_year - DEFAULT_BACKFILL_YEARS
    log.info("抓取 BLS 總經最新資料 %s ~ %s ...", start_year, end_year)
    series_map = fetch_series(list(MACRO_SERIES.keys()), start_year, end_year)
    result = _store_all(series_map)
    log.info("BLS 總經資料更新完成：%s", result)
    return result


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
