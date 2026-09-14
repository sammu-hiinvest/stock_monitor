"""野村投信 (nomurafunds.com.tw) 申購買回清單 scraper。

API: POST https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundTradeInfo
Body: {"Type":1,"Keyword":"","FundNo":"<代碼>","Date":"2026/09/01"}
適用：00985A / 00980A

已知問題與修正說明：
nomurafunds.com.tw 的憑證鏈中間憑證(TWCA)缺少 Subject Key Identifier 擴充欄位。
OpenSSL 3.2+ 預設開啟的 X509_STRICT 檢查會因此直接判憑證鏈無效而連線失敗
(SSLCertVerificationError: Missing Subject Key Identifier)，但憑證本身沒有過期、
主機名稱也吻合，瀏覽器與舊版 OpenSSL 都能正常信任這條鏈——這是對方網站憑證設定
不夠完整、而不是憑證造假或中間人攻擊的跡象。

因此這裡只關掉這一項過嚴的 RFC 5280 檢查 (ssl.VERIFY_X509_STRICT)，其餘憑證驗證
(信任鏈、到期日、主機名稱比對) 全部維持正常開啟，不等同於停用 SSL 驗證。
"""

import ssl

from requests.adapters import HTTPAdapter

from app.scrapers.common import make_session, parse_any_date, read_json, to_float, today_slash

URL = "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundTradeInfo"


class _RelaxedStrictnessAdapter(HTTPAdapter):
    """只關閉 VERIFY_X509_STRICT，其餘憑證驗證維持預設開啟。"""

    def _build_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_context()
        return super().proxy_manager_for(*args, **kwargs)

_ASSET_GROUPS = {
    "Stocks": ("STOCK", "CStockCode", "CStockName"),
    "Futures": ("FUTURES", "CFuturesCode", "CFuturesName"),
    "Bonds": ("BOND", "CBondCode", "CBondName"),
    "Etfs": ("ETF", "CEtfCode", "CEtfName"),
    "Options": ("OTHER", "COptionCode", "COptionName"),
}


def fetch(fund_no: str) -> dict:
    session = make_session()
    session.mount("https://www.nomurafunds.com.tw", _RelaxedStrictnessAdapter())
    resp = session.post(
        URL,
        json={"Type": 1, "Keyword": "", "FundNo": fund_no, "Date": today_slash()},
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "Referer": "https://www.nomurafunds.com.tw/ETFWEB/pcf",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = read_json(resp)
    entries = data.get("Entries") or {}

    # CPcfdate 是公告/查詢日 (通常是今天)，CNavDt 才是持股與淨值實際反映的交易基準日，
    # 要用 CNavDt 才會跟其他投信的資料日期定義一致。
    trade_date = parse_any_date(entries.get("CNavDt")) or parse_any_date(entries.get("CPcfdate"))

    holdings = []
    for key, (asset_type, code_field, name_field) in _ASSET_GROUPS.items():
        for row in entries.get(key) or []:
            holdings.append(
                {
                    "asset_type": asset_type,
                    "code": str(row.get(code_field, "")).strip(),
                    "name": str(row.get(name_field, "")).strip(),
                    "shares": to_float(row.get("CQuantity")),
                    "weight_pct": to_float(row.get("CWeightsPct")),
                    "market_value": None,
                }
            )

    return {
        "date": trade_date,
        "nav_per_unit": to_float(entries.get("CAnceNav")),
        "total_units": to_float(entries.get("CAnceTotalIssues")),
        "units_diff": to_float(entries.get("CAnceIssuesDiff")),
        "net_asset_value": to_float(entries.get("CAnceTotalAv")),
        "beneficiaries_count": to_float(entries.get("CBeneficiariesCount")),
        "holdings": holdings,
        "raw": data,
    }
