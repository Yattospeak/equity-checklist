# -*- coding: utf-8 -*-
"""
数据源层：统一封装 A 股 / 港股 的数据接口。

设计原则（多源冗余 + 优雅降级）：
  1. 每个数据域都有主源与备源，任一失败不中断整体流程
  2. 所有请求带超时、重试、UA、限流
  3. 返回结构统一，调用方无需感知来自哪个源

已验证可用源：
  - 腾讯 qt.gtimg.cn      实时行情（A股/港股）
  - 腾讯 smartbox         名称/代码/拼音 -> 证券代码
  - 新浪 openapi           财报三表（字段最全，含商誉/合同负债/交易性金融资产）
  - 东财 datacenter        DMSK 三表（数值规范，支持批量，用于交叉校验与全市场扫描）
  - 东财 datacenter        分红、股东户数
  - 巨潮 cninfo            公告列表
"""

import json
import random
import time
import urllib.request
from typing import Any, Dict, List, Optional

import requests

# 诚实标识：不使用伪装成浏览器的 UA，便于数据源方识别并限流。
# 已实测腾讯行情 / 腾讯 smartbox / 新浪财报 / 东财 datacenter / 巨潮资讯
# 五个接口在诚实 UA 下均正常返回，与浏览器 UA 结果一致。
UA = "equity-checklist/2.2 (+https://github.com/Yattospeak/equity-checklist)"

DEFAULT_TIMEOUT = 20


# --------------------------------------------------------------------------
# 基础 HTTP
# --------------------------------------------------------------------------
class HttpClient:
    """带限流与重试的会话。东财对频率敏感，需串行限速。"""

    def __init__(self, min_interval: float = 0.6, retries: int = 2):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.min_interval = min_interval
        self.retries = retries
        self._last = 0.0
        self.log: List[str] = []

    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0.05, 0.25))

    def get(self, url, params=None, headers=None, timeout=DEFAULT_TIMEOUT, **kw):
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                self._throttle()
                r = self.session.get(url, params=params, headers=headers,
                                     timeout=timeout, **kw)
                self._last = time.time()
                return r
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"GET 失败 {url}: {last_err}")

    def post(self, url, data=None, headers=None, timeout=DEFAULT_TIMEOUT, **kw):
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                self._throttle()
                r = self.session.post(url, data=data, headers=headers,
                                      timeout=timeout, **kw)
                self._last = time.time()
                return r
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"POST 失败 {url}: {last_err}")


HTTP = HttpClient()


def _get_json(url, params=None, headers=None, timeout=DEFAULT_TIMEOUT):
    r = HTTP.get(url, params=params, headers=headers, timeout=timeout)
    return r.json()


# --------------------------------------------------------------------------
# 代码解析：名称 / 拼音 / 代码 -> {market, code, name}
# --------------------------------------------------------------------------
MARKET_NAME = {"sh": "上交所", "sz": "深交所", "bj": "北交所", "hk": "港交所", "us": "美股"}


def unescape(s: str) -> str:
    """
    smartbox 返回形如 v_hint="sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A"，
    其中的 \\uXXXX 是字面量（响应体非 JSON，requests 不会自动解码），需手动还原。
    """
    if not s or "\\u" not in s:
        return s
    try:
        return json.loads('"' + s.replace('"', '\\"') + '"')
    except Exception:  # noqa: BLE001
        return s


def resolve_symbol(query: str) -> Optional[Dict[str, Any]]:
    """
    把用户任意输入解析为证券。

    支持：'贵州茅台' / '茅台' / '600519' / 'sh600519' / 'gzmt' / '00700' / '腾讯控股'
    优先 A 股（本项目主市场），其次港股。
    """
    q = (query or "").strip()
    if not q:
        return None

    candidates: List[Dict[str, Any]] = []

    # 纯 6 位数字：A 股代码，直接推断市场，无需联网
    if q.isdigit() and len(q) == 6:
        market = "sh" if q[0] in "69" else ("bj" if q[0] in "48" else "sz")
        candidates.append({"market": market, "code": q, "name": "", "source": "infer"})

    try:
        r = HTTP.get("https://smartbox.gtimg.cn/s3/", params={"q": q, "t": "all"},
                     timeout=10)
        r.encoding = "utf-8"
        text = r.text
        if "=" in text and '"' in text:
            body = text.split('"')[1]
            for seg in body.split("^"):
                parts = seg.split("~")
                if len(parts) < 4:
                    continue
                candidates.append({
                    "market": parts[0], "code": parts[1], "name": unescape(parts[2]),
                    "pinyin": parts[3] if len(parts) > 3 else "",
                    "source": "smartbox",
                })
    except Exception:  # noqa: BLE001
        pass

    if not candidates:
        return None

    # 排序优先级：A股 > 港股 > 美股；且排除带 r 的（融资融券/人民币柜台等衍生条目）
    def rank(c):
        m = c["market"]
        base = {"sh": 0, "sz": 0, "bj": 0, "hk": 1, "us": 2}.get(m, 3)
        # 港股代码带 r 前缀（如 80700）是人民币柜台，降权
        if m == "hk" and c["code"].startswith("8"):
            base += 0.5
        return base

    candidates.sort(key=rank)
    best = candidates[0]

    # 若推断式解析（纯数字），补名字
    if best.get("source") == "infer":
        try:
            q1 = quote([f"{best['market']}{best['code']}"])
            if q1:
                best["name"] = list(q1.values())[0].get("name", "")
        except Exception:  # noqa: BLE001
            pass
    return best


def to_secucode(market: str, code: str) -> str:
    """转成东财 SECUCODE：600519.SH / 00700.HK"""
    m = (market or "").lower()
    if m == "hk":
        return f"{code.zfill(5)}.HK"
    if m == "bj":
        return f"{code}.BJ"
    if m == "sz":
        return f"{code}.SZ"
    return f"{code}.SH"


# --------------------------------------------------------------------------
# 行情
# --------------------------------------------------------------------------
def _tencent_prefix(market: str, code: str) -> str:
    m = (market or "").lower()
    if m == "hk":
        return f"hk{code.zfill(5)}"
    return f"{m}{code}"


def quote(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    批量行情。symbols 形如 ['sh600519','hk00700']。
    返回 {code: {name, price, pe_ttm, pb, mcap_yi, ...}}
    """
    if not symbols:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="ignore")

    out: Dict[str, Dict[str, Any]] = {}
    for line in raw.strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        v = line.split('"')[1].split("~")
        if len(v) < 10:
            continue
        code = key[2:]
        is_hk = key.startswith("hk")

        def f(idx, default=0.0):
            try:
                return float(v[idx]) if idx < len(v) and v[idx] not in ("", "-") else default
            except (ValueError, IndexError):
                return default

        rec = {
            "name": v[1] if len(v) > 1 else "",
            "price": f(3),
            "change_pct": f(32) if not is_hk else f(31),
            "currency": "HKD" if is_hk else "CNY",
        }
        if is_hk:
            # 港股字段布局与 A 股不同
            rec.update({"pe_ttm": f(45), "mcap_yi": f(44), "pb": f(46)})
        else:
            rec.update({"pe_ttm": f(39), "mcap_yi": f(44), "pb": f(46),
                        "pe_static": f(52) if len(v) > 52 else 0.0,
                        "turnover_pct": f(38)})
        out[code] = rec
    return out


# --------------------------------------------------------------------------
# 财报：主源新浪（字段全），备源东财 DMSK（可批量，数值规范）
# --------------------------------------------------------------------------
SINA_URL = ("https://quotes.sina.cn/cn/api/openapi.php/"
            "CompanyFinanceService.getFinanceReport2022")
REPORT_MAP = {"income": "lrb", "balance": "fzb", "cashflow": "llb"}


def sina_statements(market: str, code: str, periods: int = 12) -> Dict[str, List[Dict]]:
    """新浪财报三表。返回 {'income':[...], 'balance':[...], 'cashflow':[...]}"""
    if market == "hk":
        return {}  # 新浪该接口仅覆盖 A 股
    prefix = "sh" if market in ("sh", "bj") else "sz"
    out: Dict[str, List[Dict]] = {}
    for key, rt in REPORT_MAP.items():
        try:
            r = HTTP.get(SINA_URL, params={
                "paperCode": f"{prefix}{code}", "source": rt,
                "type": "0", "page": "1", "num": str(periods)}, timeout=20)
            rl = (r.json().get("result", {}).get("data", {}).get("report_list", {}) or {})
            rows = []
            for period in sorted(rl.keys(), reverse=True)[:periods]:
                rec = {"报告期": f"{period[:4]}-{period[4:6]}-{period[6:8]}"}
                for it in rl[period].get("data", []) or []:
                    t = it.get("item_title")
                    if not t or it.get("item_value") is None:
                        continue
                    rec[t] = it.get("item_value")
                    tb = it.get("item_tongbi")
                    if tb not in (None, ""):
                        rec[t + "_同比"] = tb
                rows.append(rec)
            out[key] = rows
        except Exception:  # noqa: BLE001
            out[key] = []
    return out


EM_REPORTS = {
    "income": "RPT_DMSK_FN_INCOME",
    "balance": "RPT_DMSK_FN_BALANCE",
    "cashflow": "RPT_DMSK_FN_CASHFLOW",
}
EM_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def em_datacenter(report_name: str, columns: str = "ALL", filter_str: str = "",
                  page_size: int = 50, sort_columns: str = "", sort_types: str = "-1"):
    try:
        r = HTTP.get(EM_DATACENTER, params={
            "reportName": report_name, "columns": columns, "filter": filter_str,
            "pageNumber": "1", "pageSize": str(page_size),
            "sortColumns": sort_columns, "sortTypes": sort_types,
            "source": "WEB", "client": "WEB"}, timeout=20)
        d = r.json()
        if d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
    except Exception:  # noqa: BLE001
        pass
    return []


def em_statements(secucode: str, periods: int = 12) -> Dict[str, List[Dict]]:
    """东财 DMSK 三表（A 股）。数值规范，支持批量 filter。"""
    out: Dict[str, List[Dict]] = {}
    for key, rn in EM_REPORTS.items():
        rows = em_datacenter(rn, filter_str=f'(SECUCODE="{secucode}")',
                             page_size=periods, sort_columns="REPORT_DATE",
                             sort_types="-1")
        out[key] = rows or []
    return out


# --------------------------------------------------------------------------
# 分红 / 股东户数 / 公告
# --------------------------------------------------------------------------
def dividend_history(code: str, page_size: int = 25) -> List[Dict]:
    """
    分红派息历史。

    注意：PRETAX_BONUS_RMB 单位是「每 10 股派息（元，税前）」，
    已在 dps 字段换算为每股（bonus_rmb / 10）。
    """
    rows = em_datacenter("RPT_SHAREBONUS_DET",
                         filter_str=f'(SECURITY_CODE="{code}")',
                         page_size=page_size,
                         sort_columns="EX_DIVIDEND_DATE", sort_types="-1")
    out = []
    for r in rows:
        b = r.get("PRETAX_BONUS_RMB", 0) or 0
        try:
            dps = round(float(b) / 10.0, 6)  # 每10股 -> 每股
        except (TypeError, ValueError):
            dps = 0.0
        out.append({
            "date": str(r.get("EX_DIVIDEND_DATE", ""))[:10],
            "bonus_rmb": b,          # 原始：每10股派息
            "dps": dps,              # 换算：每股派息
            "plan": r.get("ASSIGN_PROGRESS", ""),
            "report_date": str(r.get("REPORT_DATE", ""))[:10],
            "dividend_yield": r.get("DIVIDENT_RATIO", 0),
        })
    return out


def holder_num(code: str, page_size: int = 10) -> List[Dict]:
    rows = em_datacenter("RPT_HOLDERNUMLATEST",
                         filter_str=f'(SECURITY_CODE="{code}")',
                         page_size=page_size,
                         sort_columns="END_DATE", sort_types="-1")
    return [{
        "date": str(r.get("END_DATE", ""))[:10],
        "holder_num": r.get("HOLDER_NUM", 0),
        "change_ratio": r.get("HOLDER_NUM_RATIO", 0),
    } for r in rows]


def announcements(code: str, page_size: int = 60) -> List[Dict]:
    """巨潮公告列表。"""
    from datetime import datetime

    def ts2d(ts):
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        return str(ts)[:10] if ts else ""

    payload = {
        "stock": f"{code},gssh0{code}", "tabName": "fulltext",
        "pageSize": str(page_size), "pageNum": "1", "column": "", "category": "",
        "plate": "", "seDate": "", "searchkey": "", "secid": "",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    headers = {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
               "Referer": "https://www.cninfo.com.cn/new/disclosure",
               "Origin": "https://www.cninfo.com.cn"}
    try:
        r = HTTP.post("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                      data=payload, headers=headers, timeout=20)
        items = r.json().get("announcements") or []
    except Exception:  # noqa: BLE001
        return []
    return [{
        "title": i.get("announcementTitle", ""),
        "type": i.get("announcementTypeName", ""),
        "date": ts2d(i.get("announcementTime")),
        "url": ("https://static.cninfo.com.cn/" + i["adjunctUrl"]) if i.get("adjunctUrl") else "",
    } for i in items]


def consensus(code: str) -> Dict[str, Any]:
    """机构一致预期（同花顺）。失败返回空结构。"""
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    try:
        r = HTTP.get(url, headers={"User-Agent": UA,
                                   "Referer": "https://basic.10jqka.com.cn/"}, timeout=20)
        r.encoding = "gbk"
        from io import StringIO
        import pandas as pd
        dfs = pd.read_html(StringIO(r.text))
        if not dfs:
            return {}
        df = dfs[0]
        recs = []
        for _, row in df.head(6).iterrows():
            vals = [str(x) for x in row.tolist()]
            joined = " | ".join(vals)
            recs.append(joined)
        return {"raw": recs[:6]}
    except Exception:  # noqa: BLE001
        return {}
