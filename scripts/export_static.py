"""GitHub Pages 靜態展示版匯出腳本。

把 FastAPI app 會回傳的所有 JSON 資料，用 TestClient 在 process 內直接呼叫(不用真
的起一個 uvicorn server、也不用真的打網路)，存成一堆靜態 .json 檔案到 docs/ 資料
夾，並把 app/static/index.html 複製一份到 docs/index.html、注入
`window.STATIC_MODE = true`，讓前端的 api() 改讀這些靜態檔案而不是打即時的
/api/... 端點 — 本機用 FastAPI 開的 app/static/index.html 完全不受影響(那份檔案
沒有 STATIC_MODE 這個全域變數，api() 走原本 fetch(path) 的行為)。

涵蓋範圍 = 前端實際會呼叫到的組合：
- 沒有動態參數的端點：照抄前端寫死的 URL 直接匯出一次(債市/Macros/Bond Auction
  這幾頁的圖表都是固定 ids=... 的查詢，沒有使用者可調的下拉選單)。
- 有動態參數的端點 (ticker/code/date/product...)：先呼叫對應的「列表」端點
  (funds/stocks/txo dates/contract-months/futures dates) 撈出全部可能值，再
  窮舉組合匯出。ETF 持股明細、期貨表格、個股歷史這些「下拉選單挑單一值」的
  端點全部窮舉；但 TXO 的 compare(兩個日期兩兩配對) 只匯出頁面預設顯示的那組
  「全部合約、最早~最新日期」，因為真的兩兩窮舉會是 O(n^2) 組合、不成比例地
  膨脹匯出檔案數量，而使用者主要是想看到頁面一開始長什麼樣子，不是每種可能
  的互動排列都要能點。

用法：
    python scripts/export_static.py

跟 GitHub Actions 的 .github/workflows/daily-fetch.yml 串在一起：每天抓完新資料
後會呼叫這支腳本重新產生 docs/，GitHub Pages 設定成從 main branch 的 /docs 資料夾
發布，資料跟展示頁就會一起每天自動更新。
"""

import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(ROOT, "docs")
API_DATA_DIR = os.path.join(DOCS_DIR, "api_data")

CHART_JS_TAG = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>'


def _static_path(url: str) -> str:
    """跟前端 index.html 的 staticApiPath() 邏輯必須完全對應，兩邊路徑算法不一致
    的話靜態頁面在瀏覽器上會全部 404，看起來卻不會有任何 Python 端的錯誤。

    存檔名稱一定要用「解碼過」的字面字串(不能有 %XX)：靜態檔案伺服器收到請求時
    會先把路徑 percent-decode 一輪才去對應實際檔案，如果檔名裡還留著編碼過的
    %XX，兩邊就會對不起來。
    """
    if "?" in url:
        route, query = url.split("?", 1)
        route = unquote(route)
        return os.path.join(API_DATA_DIR, route.lstrip("/") + "__" + query + ".json")
    return os.path.join(API_DATA_DIR, unquote(url).lstrip("/") + ".json")


def _fetch_and_save(client: TestClient, url: str, counter: list[int]):
    resp = client.get(url)
    resp.raise_for_status()
    data = resp.json()
    path = _static_path(url)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    counter[0] += 1
    return data


def export_api_data(client: TestClient) -> int:
    n = [0]
    fetch = lambda url: _fetch_and_save(client, url, n)  # noqa: E731

    # --- ETF 總覽 / 個股跨ETF比較 ---
    funds = fetch("/api/funds")
    for f in funds:
        ticker = f["ticker"]
        fetch(f"/api/funds/{ticker}/summary?days=365")
        dates = fetch(f"/api/funds/{ticker}/dates")
        stocks = fetch(f"/api/funds/{ticker}/stocks")
        for d in dates:
            fetch(f"/api/funds/{ticker}/holdings?date={d}")
            fetch(f"/api/funds/{ticker}/holdings/daily-changes?date={d}")
        for s in stocks:
            fetch(f"/api/funds/{ticker}/stocks/{quote(s['code'])}/history")

    all_stocks = fetch("/api/stocks")
    for s in all_stocks:
        fetch(f"/api/stocks/{quote(s['code'])}/price")
        fetch(f"/api/stocks/{quote(s['code'])}/events")

    # --- 法人現貨 ---
    fetch("/api/institutional-stock/ranking?top_n=50")

    # --- 台指選擇權 (TXO)：只匯出頁面預設顯示的那組(全部合約、最早~最新日期) ---
    txo_dates = fetch("/api/txo/dates")
    fetch("/api/txo/contract-months")
    if txo_dates:
        earliest, latest = txo_dates[-1], txo_dates[0]
        fetch(f"/api/txo/summary?start={earliest}&end={latest}")
        fetch(f"/api/txo/oi-range?start={earliest}&end={latest}")
        fetch(f"/api/txo/compare?date_a={earliest}&date_b={latest}")

    # --- 大盤期貨 ---
    fut_dates = fetch("/api/futures/dates")
    if fut_dates:
        for d in fut_dates:
            fetch(f"/api/futures/table?date={d}")
        earliest, latest = fut_dates[-1], fut_dates[0]
        fetch(f"/api/futures/net-ratio?product=MTX&start={earliest}&end={latest}")
        fetch(f"/api/futures/net-ratio?product=TMF&start={earliest}&end={latest}")

    # --- 債市 ---
    for url in [
        "/api/bonds/latest",
        "/api/bonds/yield-curve",
        "/api/bonds/series?ids=DGS10,ACMY10,ACMTP10",
        "/api/bonds/series?ids=T10Y2Y,T10Y3M",
        "/api/bonds/series?ids=BAMLC0A4CBBB,BAMLH0A0HYM2",
        "/api/bonds/series?ids=T5YIFR,T5YIE",
        "/api/bonds/policy-expectation",
        "/api/bonds/mpt-probability",
        "/api/bonds/inflation",
        "/api/bonds/taylor-rule",
    ]:
        fetch(url)

    # --- Macros ---
    for url in [
        "/api/macro/stats",
        "/api/macro/ppi",
        "/api/macro/payroll",
        "/api/macro/household-rates",
        "/api/macro/unemployment-reason",
        "/api/macro/unemployment-duration",
        "/api/macro/parttime",
    ]:
        fetch(url)

    # --- Bond Auction ---
    for url in [
        "/api/bonds/series?ids=AUCTION_10Y_AMOUNT,AUCTION_10Y_BTC",
        "/api/bonds/series?ids=AUCTION_30Y_AMOUNT,AUCTION_30Y_BTC",
    ]:
        fetch(url)

    return n[0]


def export_html(snapshot_time: str) -> None:
    src_path = os.path.join(ROOT, "app", "static", "index.html")
    with open(src_path, "r", encoding="utf-8") as f:
        html = f.read()

    inject = (
        f'<script>window.STATIC_MODE = true; '
        f'window.STATIC_SNAPSHOT_TIME = "{snapshot_time}";</script>\n'
    )
    if CHART_JS_TAG not in html:
        raise RuntimeError("找不到 Chart.js <script> 標籤，index.html 結構可能變了，注入點需要更新")
    html = html.replace(CHART_JS_TAG, inject + CHART_JS_TAG)

    os.makedirs(DOCS_DIR, exist_ok=True)
    out_path = os.path.join(DOCS_DIR, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    # 每次重新匯出前先清空舊的 api_data/，避免持股名單變動(某支股票被剔除、某個
    # 到期日不再是「最新」)之後，舊的靜態 JSON 檔案一直留著沒被清掉、長期累積成
    # 一堆再也用不到的孤兒檔案。
    import shutil

    shutil.rmtree(API_DATA_DIR, ignore_errors=True)

    with TestClient(app) as client:
        n = export_api_data(client)
    snapshot_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    export_html(snapshot_time)
    print(f"已匯出 {n} 個靜態 JSON 檔案到 {API_DATA_DIR}")
    print(f"已產生 {os.path.join(DOCS_DIR, 'index.html')} (快照時間 {snapshot_time})")


if __name__ == "__main__":
    main()
