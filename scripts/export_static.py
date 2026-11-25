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
import shutil
import sys
from datetime import datetime, timezone
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(ROOT, "docs")
API_DATA_DIR = os.path.join(DOCS_DIR, "api_data")
# 先完整匯出到這個暫存資料夾，全部跑完才換掉 api_data/；匯出中途就算整個程式崩潰，
# 已發布的 api_data/ 也不會被動到。
NEW_DIR = API_DATA_DIR + ".new"

CHART_JS_TAG = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>'


def _static_path(url: str, base: str | None = None) -> str:
    """跟前端 index.html 的 staticApiPath() 邏輯必須完全對應，兩邊路徑算法不一致
    的話靜態頁面在瀏覽器上會全部 404，看起來卻不會有任何 Python 端的錯誤。

    存檔名稱一定要用「解碼過」的字面字串(不能有 %XX)：靜態檔案伺服器收到請求時
    會先把路徑 percent-decode 一輪才去對應實際檔案，如果檔名裡還留著編碼過的
    %XX，兩邊就會對不起來。
    """
    base = base or API_DATA_DIR
    if "?" in url:
        route, query = url.split("?", 1)
        route = unquote(route)
        return os.path.join(base, route.lstrip("/") + "__" + query + ".json")
    return os.path.join(base, unquote(url).lstrip("/") + ".json")


def _fetch_and_save(client: TestClient, url: str, counter: list[int], failures: list[tuple[str, str, bool]]):
    """呼叫一個端點並存成靜態 JSON。單一端點失敗(500/例外)不會中止整個匯出：記錄下來，
    並盡量沿用上一次成功匯出的舊檔案(避免已發布的頁面因為一個端點暫時壞掉就整頁消失)，
    最後由 main() 彙整失敗清單、讓整支腳本以非 0 結束碼收尾，Actions 才看得到紅叉。

    過去的寫法是第一個端點出錯就整支腳本中止、而且一開始就已經把整個 api_data/ 清空，
    2026-10-21 債市資料庫被污染後 /api/bonds/yield-curve 開始 500，結果債市/Macros/Bond
    Auction 三頁的 JSON 全部沒產生(但 workflow 因為 continue-on-error 仍顯示成功)，靜態站
    這三頁因此停更超過一個月。
    """
    path = _static_path(url, NEW_DIR)
    try:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - 單一端點失敗不影響其他端點
        old_path = _static_path(url, API_DATA_DIR)
        kept = os.path.exists(old_path)
        failures.append((url, f"{type(exc).__name__}: {str(exc)[:160]}", kept))
        if not kept:
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copy2(old_path, path)
        with open(old_path, "r", encoding="utf-8") as f:
            return json.load(f)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    counter[0] += 1
    return data


def export_api_data(client: TestClient, failures: list[tuple[str, str, bool]]) -> int:
    n = [0]
    fetch = lambda url: _fetch_and_save(client, url, n, failures)  # noqa: E731

    # --- ETF 總覽 / 個股跨ETF比較 ---
    funds = fetch("/api/funds") or []
    for f in funds:
        ticker = f["ticker"]
        fetch(f"/api/funds/{ticker}/summary?days=365")
        dates = fetch(f"/api/funds/{ticker}/dates") or []
        stocks = fetch(f"/api/funds/{ticker}/stocks") or []
        for d in dates:
            fetch(f"/api/funds/{ticker}/holdings?date={d}")
            fetch(f"/api/funds/{ticker}/holdings/daily-changes?date={d}")
        for s in stocks:
            fetch(f"/api/funds/{ticker}/stocks/{quote(s['code'])}/history")

    all_stocks = fetch("/api/stocks") or []
    for s in all_stocks:
        fetch(f"/api/stocks/{quote(s['code'])}/price")
        fetch(f"/api/stocks/{quote(s['code'])}/events")

    # --- 法人現貨 ---
    fetch("/api/institutional-stock/ranking?top_n=50")

    # --- 台指選擇權 (TXO) ---
    # 日期範圍(start/end)是下拉選單可任意調整的兩個維度，真的窮舉「日期範圍 x
    # 到期月份」全部組合會爆炸，所以只匯出頁面預設顯示的日期範圍(全部合約、
    # 最早~最新日期)；但「到期月份」篩選只有 ~10幾個值、不會爆炸，所以額外把
    # 「預設日期範圍 x 每個到期月份」的組合也匯出，涵蓋使用者不動日期、只切換
    # 到期月份下拉選單的最常見操作(這也是原本沒做、導致靜態展示站切到期月份
    # 圖表不會動的那個 bug)。
    txo_dates = fetch("/api/txo/dates") or []
    txo_months = fetch("/api/txo/contract-months") or []
    if txo_dates:
        earliest, latest = txo_dates[-1], txo_dates[0]
        fetch(f"/api/txo/summary?start={earliest}&end={latest}")
        fetch(f"/api/txo/oi-range?start={earliest}&end={latest}")
        fetch(f"/api/txo/compare?date_a={earliest}&date_b={latest}")
        for m in txo_months:
            fetch(f"/api/txo/summary?start={earliest}&end={latest}&contract_month={quote(m)}")
            fetch(f"/api/txo/oi-range?start={earliest}&end={latest}&contract_month={quote(m)}")
            fetch(f"/api/txo/compare?date_a={earliest}&date_b={latest}&contract_month={quote(m)}")

    # --- 大盤期貨 ---
    fut_dates = fetch("/api/futures/dates") or []
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
        "/api/bonds/series?ids=AUCTION_52W_AMOUNT,AUCTION_52W_BTC",
        "/api/bonds/series?ids=AUCTION_2Y_AMOUNT,AUCTION_2Y_BTC",
        "/api/bonds/series?ids=AUCTION_5Y_AMOUNT,AUCTION_5Y_BTC",
        "/api/bonds/series?ids=AUCTION_7Y_AMOUNT,AUCTION_7Y_BTC",
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


def main() -> int:
    # 每次重新匯出時，api_data/ 是整個換新(不是疊加)，避免持股名單變動(某支股票被剔除、
    # 某個到期日不再是「最新」)之後，舊的靜態 JSON 檔案一直留著沒被清掉、長期累積成一堆
    # 再也用不到的孤兒檔案。但是是「匯出到暫存資料夾、全部跑完才換掉」，不是一開始就清空。
    shutil.rmtree(NEW_DIR, ignore_errors=True)
    failures: list[tuple[str, str, bool]] = []

    with TestClient(app) as client:
        n = export_api_data(client, failures)

    shutil.rmtree(API_DATA_DIR, ignore_errors=True)
    os.replace(NEW_DIR, API_DATA_DIR)

    snapshot_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    export_html(snapshot_time)
    print(f"已匯出 {n} 個靜態 JSON 檔案到 {API_DATA_DIR}")
    print(f"已產生 {os.path.join(DOCS_DIR, 'index.html')} (快照時間 {snapshot_time})")

    if failures:
        print(f"\n!! 有 {len(failures)} 個端點匯出失敗 (標 [沿用舊檔] 的表示保留了上一次的版本，其餘就是這次沒有資料)：")
        for url, reason, kept in failures[:25]:
            print(f"  {'[沿用舊檔]' if kept else '[缺檔]    '} {url}  ->  {reason}")
        if len(failures) > 25:
            print(f"  ... 另外還有 {len(failures) - 25} 個")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
