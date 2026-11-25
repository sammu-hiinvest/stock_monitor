"""美債標售 (52週國庫券／2/5/7/10年期公債Note／30年期公債Bond) 資料抓取主流程。

資料源是美國財政部 FiscalData 公開 API「Treasury Securities Auctions Data」，
不需要金鑰：
    https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query
    (資料集頁面：https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/treasury-securities-auctions-data)

用 `original_security_term`(而不是 `security_term`) 篩選 2/5/7/10/30 年期，因為
「加碼發行(reopening)」的標售在 `security_term` 會顯示成「9-Year 10-Month」這種
剩餘年期字串，只有 `original_security_term` 在整個發行週期(含加碼)都會穩定顯示
「10-Year」／「30-Year」，是正確的篩選欄位(已用實際資料驗證過)。

**52週期國庫券(T-Bill)是唯一的例外，要改用 `security_term` 篩選**：實測發現
`security_type:eq:Bill,original_security_term:eq:52-Week` 會連同同一批標售裡的
13週/26週/6週國庫券一起撈出來(這幾檔的 `original_security_term` 欄位在 FiscalData
的資料本身就填錯，不是我們篩選邏輯的問題)；反而 `security_term:eq:52-Week` 篩出來的
93筆全部都是真正的52週標售、頻率約每4週一次，跟財政部公告的發行排程吻合。Bill 不會
「加碼發行」，所以用 `security_term` 不會有 Note/Bond 那種剩餘年期的問題。

**Bid-to-cover 比率是自己算的**，不是直接拿 API 的 `bid_to_cover_ratio` 欄位：
用 `total_tendered`(總投標金額) / `total_accepted`(實際得標金額)。已跟 API
本身提供的 `bid_to_cover_ratio` 欄位交叉比對過，兩者數值一致，這裡仍選擇自己
算是為了計算方式透明、不依賴上游欄位是否每一筆都有填值(較舊的標售資料這個
欄位常是 null)。

存進跟其他債市資料共用的 `bond_series` 表，每個年期各拆成「標售金額」跟
「bid-to-cover比率」兩條序列 (`AUCTION_52W_AMOUNT`/`AUCTION_52W_BTC`、
`AUCTION_2Y_AMOUNT`/`AUCTION_2Y_BTC`、`AUCTION_5Y_AMOUNT`/`AUCTION_5Y_BTC`、
`AUCTION_7Y_AMOUNT`/`AUCTION_7Y_BTC`、`AUCTION_10Y_AMOUNT`/`AUCTION_10Y_BTC`、
`AUCTION_30Y_AMOUNT`/`AUCTION_30Y_BTC`)，讓前端可以直接沿用既有的通用端點
`/api/bonds/series?ids=...`，不需要新增專屬 API。標售金額存成「十億美元」
(除以 1e9)方便圖表顯示，不存原始美元數字。
"""

import logging
from datetime import date, datetime, timedelta

import requests

from app.db import init_db, session, upsert_bond_series

log = logging.getLogger("fetch_auctions")

AUCTIONS_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"

# key -> (security_type, 篩選欄位, 篩選值, 標售金額序列代碼, BTC比率序列代碼)
# 依年期由短到長排序，跟前端圖表的排列順序一致。
AUCTION_TERMS: dict[str, tuple[str, str, str, str, str]] = {
    "52W_BILL": ("Bill", "security_term", "52-Week", "AUCTION_52W_AMOUNT", "AUCTION_52W_BTC"),
    "2Y_NOTE": ("Note", "original_security_term", "2-Year", "AUCTION_2Y_AMOUNT", "AUCTION_2Y_BTC"),
    "5Y_NOTE": ("Note", "original_security_term", "5-Year", "AUCTION_5Y_AMOUNT", "AUCTION_5Y_BTC"),
    "7Y_NOTE": ("Note", "original_security_term", "7-Year", "AUCTION_7Y_AMOUNT", "AUCTION_7Y_BTC"),
    "10Y_NOTE": ("Note", "original_security_term", "10-Year", "AUCTION_10Y_AMOUNT", "AUCTION_10Y_BTC"),
    "30Y_BOND": ("Bond", "original_security_term", "30-Year", "AUCTION_30Y_AMOUNT", "AUCTION_30Y_BTC"),
}

# 只保留最近這麼多天的標售資料，跟 ACM Term Premium 圖表一樣至少涵蓋10年(10年+1年緩衝)。
KEEP_DAYS = 365 * 11


def _fetch_term(security_type: str, term_field: str, term_value: str, start: date) -> list[dict]:
    """分頁抓完某個年期的全部標售紀錄(通常一年期一次 API 查詢就抓得完，這裡保留
    分頁邏輯是為了保險，避免哪天資料量變多超過單頁上限時默默漏資料)。
    """
    rows: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            AUCTIONS_URL,
            params={
                "filter": (
                    f"security_type:eq:{security_type},"
                    f"{term_field}:eq:{term_value},"
                    f"auction_date:gte:{start.isoformat()}"
                ),
                "fields": "auction_date,total_accepted,total_tendered",
                "sort": "auction_date",
                "page[number]": page,
                "page[size]": 200,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows.extend(payload["data"])
        if page >= payload["meta"]["total-pages"]:
            break
        page += 1
    return rows


def _process(security_type: str, term_field: str, term_value: str, start: date) -> tuple[list[dict], list[dict]]:
    raw = _fetch_term(security_type, term_field, term_value, start)
    amount_rows, btc_rows = [], []
    for r in raw:
        obs_date = r["auction_date"]
        accepted = r.get("total_accepted")
        tendered = r.get("total_tendered")
        if accepted in (None, "null", ""):
            continue
        accepted = float(accepted)
        amount_rows.append({"obs_date": obs_date, "value": round(accepted / 1e9, 3)})
        if tendered not in (None, "null", "") and accepted:
            btc_rows.append({"obs_date": obs_date, "value": round(float(tendered) / accepted, 3)})
    return amount_rows, btc_rows


TD_URL = "https://www.treasurydirect.gov/TA_WS/securities/auctioned"
_TD_FIELD = {"security_term": "securityTerm", "original_security_term": "originalSecurityTerm"}


def _fetch_treasurydirect(security_type: str) -> list[dict]:
    resp = requests.get(TD_URL, params={"format": "json", "type": security_type}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"TreasuryDirect {security_type}: 回應不是清單 ({str(data)[:80]!r})")
    return data


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _treasurydirect_rows(start: date) -> dict[str, tuple[list[dict], list[dict]]]:
    """從 TreasuryDirect 官方標售結果 API 算出跟 FiscalData 同定義的金額/BTC 序列。

    **為什麼需要**：FiscalData 的 auctions_query 資料集在 2026-10 初停止回填結果——全資料集
    最新一筆是 10/22，而且 `total_accepted` 全是 null，各年期最後有結果的標售都停在 10/8
    以前。標售頁因此從 10 月起完全沒有新資料(11 月的 2/5/7/10/30 年期與 52 週標售都沒有)。
    TreasuryDirect 同一批標售的結果是齊全的。

    **定義已逐筆對帳**：`grandTotalParAwarded`(得標總額) = FiscalData 的 total_accepted、
    `grandTotalParRecognized`(投標總額) = total_tendered，用 `competitiveAuctionCloseDate`
    當標售日。2026-01~10 共 353 筆重疊標售，金額跟 BTC 全部一致、零差異。**不要用 API 的
    `bidToCoverRatio`**：那是排除 SOMA 加碼認購、以公告發行額計算的版本，跟頁面一貫的
    「總投標 ÷ 總得標」定義不同(例如 11/24 五年期是 2.37，頁面口徑是 2.24)。

    API 只回傳最近 ~250 筆(Note/Bond)，用來補最近幾個月的缺口足夠；更早的歷史仍由
    FiscalData 提供。
    """
    out: dict[str, tuple[list[dict], list[dict]]] = {}
    by_type: dict[str, list[dict]] = {}
    for key, (sec_type, term_field, term_value, amount_sid, btc_sid) in AUCTION_TERMS.items():
        if sec_type not in by_type:
            by_type[sec_type] = _fetch_treasurydirect(sec_type)
        amount_rows, btc_rows = [], []
        for r in by_type[sec_type]:
            if r.get(_TD_FIELD[term_field]) != term_value:
                continue
            obs_date = (r.get("competitiveAuctionCloseDate") or "")[:10]
            awarded, recognized = _num(r.get("grandTotalParAwarded")), _num(r.get("grandTotalParRecognized"))
            if not obs_date or obs_date < start.isoformat() or not awarded:
                continue  # 尚未開標的標售(結果欄位是空的)
            amount_rows.append({"obs_date": obs_date, "value": round(awarded / 1e9, 3)})
            if recognized:
                btc_rows.append({"obs_date": obs_date, "value": round(recognized / awarded, 3)})
        out[key] = (amount_rows, btc_rows)
    return out


def run_latest() -> dict:
    """排程/手動用：FiscalData API 沒有增量端點，每次都整段區間重抓並覆寫；再用
    TreasuryDirect 補上 FiscalData 沒有結果的標售。每個年期、每個來源各自 try/except，
    一個失敗不影響其他。"""
    init_db()
    start = date.today() - timedelta(days=KEEP_DAYS)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    result = {}
    fiscal_dates: dict[str, set[str]] = {}
    with session() as conn:
        for key, (sec_type, term_field, term_value, amount_sid, btc_sid) in AUCTION_TERMS.items():
            log.info("下載美債標售資料 %s (%s / %s=%s) ...", key, sec_type, term_field, term_value)
            try:
                amount_rows, btc_rows = _process(sec_type, term_field, term_value, start)
            except Exception as exc:  # noqa: BLE001 - 單一年期失敗不影響其他年期
                log.exception("FiscalData %s 抓取失敗: %s", key, exc)
                result[amount_sid] = f"error: {exc}"
                continue
            fiscal_dates[key] = {r["obs_date"] for r in amount_rows}
            for r in amount_rows:
                r["fetched_at"] = fetched_at
            for r in btc_rows:
                r["fetched_at"] = fetched_at
            upsert_bond_series(conn, amount_sid, amount_rows)
            upsert_bond_series(conn, btc_sid, btc_rows)
            result[amount_sid] = len(amount_rows)
            result[btc_sid] = len(btc_rows)

        # --- 補資料：FiscalData 沒有結果的標售日，改用 TreasuryDirect ---
        try:
            added = 0
            for key, (amount_rows, btc_rows) in _treasurydirect_rows(start).items():
                _, _, _, amount_sid, btc_sid = AUCTION_TERMS[key]
                have = fiscal_dates.get(key, set())
                new_amount = [r for r in amount_rows if r["obs_date"] not in have]
                new_btc = [r for r in btc_rows if r["obs_date"] not in have]
                for r in new_amount + new_btc:
                    r["fetched_at"] = fetched_at
                upsert_bond_series(conn, amount_sid, new_amount)
                upsert_bond_series(conn, btc_sid, new_btc)
                added += len(new_amount)
            result["treasurydirect_added_auctions"] = added
        except Exception as exc:  # noqa: BLE001
            log.exception("TreasuryDirect 補資料失敗: %s", exc)
            result["treasurydirect_error"] = str(exc)
    log.info("美債標售資料更新完成：%s", result)
    return result


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
