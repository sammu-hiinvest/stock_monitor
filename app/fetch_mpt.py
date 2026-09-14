"""Atlanta Fed「Market Probability Tracker (MPT)」歷史機率資料抓取。

資料來源是 Atlanta Fed 官方公開下載的歷史資料 Excel 檔（不是逆向工程找到的隱藏
端點，是官網 Market Probability Tracker 頁面上明列的「MPT Historical Data」下載
連結）：
    https://www.atlantafed.org/-/media/Project/Atlanta/FRBA/Documents/cenfis/
    market-probability-tracker/mpt_histdata.xlsx

背後是 CME 3個月期 SOFR 選擇權隱含的機率分布（CME 官方期貨/選擇權報價本身要付費
訂閱、且有反爬蟲保護，直接連線會被拒絕，這是先前嘗試過的結論）；但 Atlanta Fed
把處理過的統計量整理成這份公開 Excel，供研究/教育用途下載，不需要 API 金鑰。

**授權限制**（見檔案內 LICENSE 分頁的文字方塊，openpyxl 讀不到、要直接解析
xl/drawings/drawing1.xml 才看得到）："Use of this data is permitted for personal
and educational purposes only." 本專案是本機個人使用的儀表板，屬於這個授權範圍
內；這裡只計算、儲存由這份資料算出的精簡衍生時間序列（每個觀測日對應「最近一次
到期窗口」的升息/降息機率），不會整包重新發布/散布原始 Excel 檔案本身。

原始 DATA 分頁是長格式，30 萬多列，欄位為：
    date            觀測日 (該機率是「哪一天」估的)
    reference_start CME 3個月SOFR合約對應的參考窗口起始日 (近似「哪一次FOMC會議」)
    target_range    觀測日當下的官方聯邦資金目標利率區間
    field           統計量名稱，如 "Prob: cut" / "Prob: hike" / "Rate: mean" 等
    value           數值 (機率型欄位是百分比 0~100)

處理邏輯：對每個觀測日，取「reference_start >= 觀測日」中最小的那一個（也就是
最近的下一個到期窗口），抓該窗口的 Prob: cut / Prob: hike 兩個欄位，整理成
(date, reference_meeting, prob_cut, prob_hike) 的精簡序列存進 SQLite 的
mpt_probability 表，避免每次 API 請求都要重新讀取這個 60多MB、30萬多列的 Excel
(用 pandas 讀取要 20~30 秒)。Atlanta Fed 只提供整包下載、沒有增量端點，所以每次
執行都是整包重抓，但存進 DB 的結果精簡(每個觀測日一列)，upsert 很快。
"""

import io
import logging
from datetime import datetime

import pandas as pd
import requests

from app.db import init_db, session, upsert_mpt_probability

log = logging.getLogger("fetch_mpt")

MPT_URL = (
    "https://www.atlantafed.org/-/media/Project/Atlanta/FRBA/Documents/"
    "cenfis/market-probability-tracker/mpt_histdata.xlsx"
)


def _download_and_process() -> list[dict]:
    resp = requests.get(MPT_URL, timeout=60)
    resp.raise_for_status()

    df = pd.read_excel(io.BytesIO(resp.content), sheet_name="DATA", engine="openpyxl")
    sub = df[df["field"].isin(["Prob: cut", "Prob: hike"])].copy()
    sub["date"] = pd.to_datetime(sub["date"])
    sub["reference_start"] = pd.to_datetime(sub["reference_start"])

    rows = []
    for d, grp in sub.groupby("date"):
        future = grp[grp["reference_start"] >= d]
        target_ref = future["reference_start"].min() if len(future) else grp["reference_start"].max()
        matched = grp[grp["reference_start"] == target_ref]
        cut = matched[matched["field"] == "Prob: cut"]["value"]
        hike = matched[matched["field"] == "Prob: hike"]["value"]
        rows.append(
            {
                "obs_date": d.date().isoformat(),
                "reference_meeting": target_ref.date().isoformat(),
                "prob_cut": float(cut.iloc[0]) if len(cut) else None,
                "prob_hike": float(hike.iloc[0]) if len(hike) else None,
            }
        )
    return rows


def run_latest() -> dict:
    """排程/手動用：重新下載整份歷史資料並覆寫。"""
    init_db()
    log.info("下載 Atlanta Fed Market Probability Tracker 歷史資料...")
    rows = _download_and_process()
    fetched_at = datetime.now().isoformat(timespec="seconds")
    for r in rows:
        r["fetched_at"] = fetched_at
    with session() as conn:
        upsert_mpt_probability(conn, rows)
    log.info("Atlanta Fed MPT 資料更新完成，共 %d 筆", len(rows))
    return {"rows": len(rows)}


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
