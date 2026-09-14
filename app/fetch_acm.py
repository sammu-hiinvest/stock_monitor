"""美債10年期 ACM Term Premium 模型資料抓取主流程。

資料源是紐約聯邦準備銀行(NY Fed)官方公開研究資料頁面「Term Premia」的整包下載
連結(不是逆向工程端點，是官網 Term Premia 互動圖表頁面「Download」按鈕背後的
同一個網址)：
    https://www.newyorkfed.org/medialibrary/media/research/data_indicators/ACMTermPremium.xls
    (頁面：https://www.newyorkfed.org/research/data_indicators/term-premia-tabs#/interactive)

背後是 Adrian-Crump-Moench (ACM) 期限結構模型，把美債殖利率拆解成「風險中立利率
預期路徑(risk-neutral yield)」+「期限溢酬(term premium)」兩部分，「ACM fitted
yield」則是模型配適出來的殖利率(理論上應該很貼近實際殖利率，用來檢驗模型配適
優劣)。三者關係：ACMY = ACMRNY + ACMTP。

檔案格式是舊版 Excel (.xls, OLE2/CDFV2 二進位格式，不是 .xlsx)，需要 `xlrd` 套件
才能用 pandas 讀取(openpyxl 只支援新版 .xlsx)。裡面有「ACM Monthly」跟「ACM Daily」
兩個分頁，本專案用「ACM Daily」(日頻資料，從 1961 年至今，跟即時性較高的殖利率/
利差資料頻率一致)。整包檔案約 10MB、涵蓋 1~10 年期共 30 個欄位，本專案只需要
10年期的 fitted yield (ACMY10) 跟 term premium (ACMTP10)，抓下來後只篩選最近
的資料存進 DB，避免 60 幾年的歷史全部塞進去。

存進跟其他債市資料共用的 bond_series 表(series_id 用 "ACMY10"/"ACMTP10"，
不會跟 FRED/BLS 的代碼撞名)，讓 `/api/bonds/series?ids=DGS10,ACMY10,ACMTP10`
可以直接沿用既有的通用查詢端點，不需要另外新增 API。
"""

import io
import logging
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from app.db import init_db, session, upsert_bond_series

log = logging.getLogger("fetch_acm")

ACM_URL = "https://www.newyorkfed.org/medialibrary/media/research/data_indicators/ACMTermPremium.xls"

# 只保留最近這麼多天的資料，避免 1961 年至今的每日資料整包塞進 DB。圖表要求至少
# 10 年的觀察窗，這裡抓 11 年(10 年 + 1 年緩衝，避免剛好卡在邊界上看起來不足10年)。
KEEP_DAYS = 365 * 11


def _download_and_process() -> dict[str, list[dict]]:
    resp = requests.get(ACM_URL, timeout=60)
    resp.raise_for_status()

    df = pd.read_excel(io.BytesIO(resp.content), sheet_name="ACM Daily", engine="xlrd")
    df = df[["DATE", "ACMY10", "ACMTP10"]].dropna(subset=["DATE"])
    df["obs_date"] = pd.to_datetime(df["DATE"]).dt.date

    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    df = df[df["obs_date"] >= cutoff]

    result: dict[str, list[dict]] = {"ACMY10": [], "ACMTP10": []}
    for _, row in df.iterrows():
        obs_date = row["obs_date"].isoformat()
        if pd.notna(row["ACMY10"]):
            result["ACMY10"].append({"obs_date": obs_date, "value": float(row["ACMY10"])})
        if pd.notna(row["ACMTP10"]):
            result["ACMTP10"].append({"obs_date": obs_date, "value": float(row["ACMTP10"])})
    return result


def run_latest() -> dict:
    """排程/手動用：NY Fed 只提供整包歷史下載、沒有增量端點，所以每次都整包
    重抓，篩選最近 KEEP_DAYS 天後 upsert，覆寫掉修訂值。
    """
    init_db()
    log.info("下載 NY Fed ACM Term Premium 資料...")
    series_map = _download_and_process()
    fetched_at = datetime.now().isoformat(timespec="seconds")
    result = {}
    with session() as conn:
        for sid, rows in series_map.items():
            for r in rows:
                r["fetched_at"] = fetched_at
            upsert_bond_series(conn, sid, rows)
            result[sid] = len(rows)
    log.info("NY Fed ACM Term Premium 資料更新完成：%s", result)
    return result


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
