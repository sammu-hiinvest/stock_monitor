"""共用工具：日期解析、HTTP session 建立等。"""

import json
import re
from datetime import date, datetime, timedelta, timezone

import requests

from app.config import HTTP_HEADERS

_ASPNET_DATE_RE = re.compile(r"/Date\((\d+)\)/")
_TAIPEI = timezone(timedelta(hours=8))


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HTTP_HEADERS)
    return s


def read_json(resp: requests.Response) -> dict:
    """安全解析 JSON 回應。

    resp.json() 會用 requests 猜測的字元編碼(apparent_encoding)去解碼，
    但當 JSON 內容以 ASCII/數字為主、中文字偏少時，統計式編碼偵測常誤判成
    ISO-8859-1 或 ascii，導致中文欄位(股票名稱等)變成亂碼。
    這裡改成固定以 UTF-8 解碼 raw bytes，四家投信 API 實測都是 UTF-8。
    """
    return json.loads(resp.content.decode("utf-8"))


def parse_any_date(value) -> str | None:
    """把各投信回傳的各種日期格式統一轉成 'YYYY-MM-DD'。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None
    value = str(value).strip()
    if not value or value.strip() in ("-", "—"):
        return None

    m = _ASPNET_DATE_RE.match(value)
    if m:
        ms = int(m.group(1))
        # 這些時間戳記是「台灣時間的午夜」(=前一天 16:00 UTC)，一定要換算回 UTC+8 才是正確
        # 日期；直接當 UTC 解析會早一天(週一變週日)，統一投信的資料日期就是因此整批錯位。
        return datetime.fromtimestamp(ms / 1000, tz=_TAIPEI).date().isoformat()

    # ISO 格式: 2026-08-31T00:00:00...
    if "T" in value:
        value = value.split("T", 1)[0]
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass

    # 純日期 2026/08/31 或 2026-08-31
    for sep in ("/", "-"):
        parts = value.split(sep)
        if len(parts) == 3:
            try:
                y, m_, d_ = (int(p) for p in parts)
                return date(y, m_, d_).isoformat()
            except ValueError:
                continue
    return None


def today_roc() -> str:
    """民國年日期字串，如 115/09/01 (統一投信 API 用)。"""
    d = date.today()
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def today_slash() -> str:
    """西元年日期字串，如 2026/09/01 (復華、野村 API 用)。"""
    d = date.today()
    return f"{d.year}/{d.month:02d}/{d.day:02d}"


def to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if value in ("", "-", "—"):
            return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
