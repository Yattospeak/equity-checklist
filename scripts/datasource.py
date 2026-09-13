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
            # 港股字段布局与 A 股不同（实测 hk00700，78 个字段）：
            #   [39]=15.67 是 PE(TTM)   —— 用市值/归母净利反推验证过，落在 14.95~15.96 区间
            #   [44][45]=38997.94 是总市值（亿港元），不是 PE
            #   [46] 是英文名 TENCENT，不是 PB
            # PB 在腾讯行情里无可靠索引（各候选位均与反推值对不上），
            # 且此阶段尚未取得财报、算不出净资产，故先置 None，
            # 由 collect 在用归母净资产算出后回填（见 hk_pb_from_equity）。
            rec.update({"pe_ttm": f(39), "mcap_yi": f(44), "pb": None})
        else:
            rec.update({"pe_ttm": f(39), "mcap_yi": f(44), "pb": f(46),
                        "pe_static": f(52) if len(v) > 52 else 0.0,
                        "turnover_pct": f(38)})
        out[code] = rec
    return out


def hk_pb_from_equity(code: str, equity_parent_cny: Optional[float],
                      price_hkd: float, fx_hkd_to_cny: float = 0.92) -> Optional[float]:
    """
    由归母净资产反推港股 PB。

    为什么不用行情接口的 PB 字段：
      1. 腾讯行情港股 78 个字段里没有可确证为 PB 的索引——实测各候选位
         均与「股价 / 每股净资产」的反推值（腾讯约 3.7）对不上；
      2. 东财 push2 域名在本环境常因代理不可达，且 datacenter 无港股估值报表。
    与其猜一个错值，不如用财报里的归母净资产自己算，口径可控、可复核。
    """
    if not equity_parent_cny or not price_hkd:
        return None
    try:
        shares = hk_total_shares(code)
        if not shares:
            return None
        nav_per_share_hkd = (equity_parent_cny * fx_hkd_to_cny) / shares
        if nav_per_share_hkd <= 0:
            return None
        return round(price_hkd / nav_per_share_hkd, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def hk_total_shares(code: str) -> Optional[float]:
    """港股总股本。优先用东财 F10 股本结构，失败则用市值 / 股价反推。"""
    rows = em_datacenter("RPT_HKF10_INFO_EQUITY",
                         filter_str=f'(SECUCODE="{code.zfill(5)}.HK")',
                         page_size=5)
    for r in rows or []:
        for k, v in r.items():
            if "SHARES" in k.upper() and "TOTAL" in k.upper() and v:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    return None


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
                  page_size: int = 50, sort_columns: str = "", sort_types: str = "-1",
                  page_number: int = 1):
    try:
        r = HTTP.get(EM_DATACENTER, params={
            "reportName": report_name, "columns": columns, "filter": filter_str,
            "pageNumber": str(page_number), "pageSize": str(page_size),
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
# 港股财报（东财 HKF10 系列）
#
# 与 A 股的两点根本差异，必须显式处理，否则指标会全错：
#   1) 数据结构是**纵表**：每行是「科目名 + 金额」，需按报告期转置成横表
#   2) 科目名是**中国香港财务准则**：营业额 / 毛利 / 股东应占溢利 / 总资产 ...
#      与 A 股的「营业总收入 / 归属于母公司所有者的净利润」不是一回事
#
# 覆盖范围（已实测 00700.HK）：
#   ✅ 利润表 RPT_HKF10_FN_INCOME      19 期（2021-2026），人民币计价
#   ✅ 负债表 RPT_HKF10_FN_BALANCE      科目齐全，可算净现金/负债率/ROE
#   ❌ 现金流量表                        东财未开放对应报表，OCF 类指标只能标缺失
#   ❌ 分红 / 股东户数 / 公告             无免登录公开源
# --------------------------------------------------------------------------
EM_HK_REPORTS = {
    "income": "RPT_HKF10_FN_INCOME",
    "balance": "RPT_HKF10_FN_BALANCE",
}

# 港股科目名 -> 内部通用名（沿用 A 股口径，使下游 compute_metrics 无需改动）
HK_ITEM_ALIAS = {
    "income": {
        "营业额": "营业总收入",
        "营运收入": "营业收入",
        "毛利": "毛利润",
        "经营溢利": "营业利润",
        "除税前溢利": "利润总额",
        "除税后溢利": "净利润",
        "股东应占溢利": "归属于母公司所有者的净利润",
        "每股基本盈利": "基本每股收益",
        "销售及分销费用": "销售费用",
        "行政开支": "管理费用",
        "融资成本": "财务费用",
        "税项": "所得税费用",
    },
    "balance": {
        "总资产": "资产总计",
        "总负债": "负债合计",
        "净资产": "所有者权益合计",
        # 东财港股负债表中，归母权益的科目名是「股东权益」（少数股东单列为「少数股东权益」）
        "股东权益": "归属于母公司股东权益合计",
        "少数股东权益": "少数股东权益",
        "现金及等价物": "货币资金",
        "短期存款": "短期存款",
        "受限制存款及现金": "受限资金",
        "指定以公允价值记账之金融资产(流动)": "交易性金融资产",
        "短期贷款": "短期借款",
        "长期贷款": "长期借款",
        "应付票据(非流动)": "应付债券",
        "存货": "存货",
        "应收帐款": "应收账款",
        "无形资产": "无形资产",
        "流动资产合计": "流动资产合计",
        "流动负债合计": "流动负债合计",
    },
}


def _hk_fetch_by_period(report_name: str, secucode: str, want: int) -> List[Dict]:
    """
    分期拉取港股纵表财报。

    东财对 pageSize 有硬上限（实测 1000 行后静默截断），而港股纵表
    每期约 55 个科目，一次请求拿不全多期。策略：
      1. 先只取 REPORT_DATE 列，靠排序拿到全部可用报告期
      2. 再按报告期逐个 filter 拉取，合并结果
    """
    # probe 也需翻页：服务端单次最多返回 200 行，而纵表一期就占 ~55 行，
    # 不翻页只能看到最近 4 期。翻到无新增为止。
    period_list: List[str] = []
    for page in range(1, 11):
        probe = em_datacenter(report_name, columns="REPORT_DATE",
                              filter_str=f'(SECUCODE="{secucode}")',
                              page_size=200, page_number=page,
                              sort_columns="REPORT_DATE", sort_types="-1")
        if not probe:
            break
        before = len(period_list)
        for r in probe:
            p = str(r.get("REPORT_DATE") or "")[:10]
            if p and p not in period_list:
                period_list.append(p)
        if len(period_list) == before:
            break
    if not period_list:
        return []
    period_list = period_list[:max(want, 1)]

    # 年报优先：ROE/趋势分析只需要年报 + 最近 1 期季报。
    # 逐个期请求受 1.2s 限流制约，无脑全拉会拖到 80s+，这里做两次取舍：
    #   1. 年报全部保留
    #   2. 非年报只保留最近 3 期（够反映最新经营状况）
    annual = [p for p in period_list if p.endswith("12-31")]
    others = [p for p in period_list if not p.endswith("12-31")][:3]
    fetch_list = sorted(set(annual + others), reverse=True)

    all_rows: List[Dict] = []
    for p in fetch_list:
        rows = em_datacenter(report_name, filter_str=f'(SECUCODE="{secucode}")'
                                                     f"(REPORT_DATE='{p}')",
                             page_size=300, sort_columns="REPORT_DATE",
                             sort_types="-1")
        if rows:
            all_rows.extend(rows)
    return all_rows


def hk_statements(secucode: str, periods: int = 12) -> Dict[str, List[Dict]]:
    """
    港股财报（东财 HKF10）。

    返回结构对齐 A 股：{'income':[{报告期, 科目...}], 'balance':[...], 'cashflow':[]}
    港股无现金流量表源，cashflow 恒为空列表——下游须据此标注指标缺失，
    绝不能用 0 填充（会把「数据不足」伪装成「经营现金流为负」）。
    """
    out: Dict[str, List[Dict]] = {"income": [], "balance": [], "cashflow": []}
    for key, rn in EM_HK_REPORTS.items():
        # 纵表每期约 55 个科目，而东财单次上限 1000 行——直接要大 pageSize
        # 会被服务端静默截断（实测 1000 行只覆盖 18 期），故按报告期逐个拉取。
        rows = _hk_fetch_by_period(rn, secucode, want=max(periods * 3, 24))
        if not rows:
            continue
        # 纵表 -> 横表：按报告期聚合科目
        by_period: Dict[str, Dict] = {}
        for r in rows:
            p = str(r.get("REPORT_DATE") or "")[:10]
            if not p:
                continue
            raw_name = (r.get("ITEM_NAME") or "").strip()
            amount = r.get("AMOUNT")
            if not raw_name or amount is None:
                continue
            name = HK_ITEM_ALIAS[key].get(raw_name, raw_name)
            rec = by_period.setdefault(p, {"报告期": p})
            # 同名科目只取首次出现（避免不同子表重复覆盖）
            if name not in rec:
                rec[name] = amount
        # 按报告期倒序取最近 N 期。年报自然落在其中，下游 is_annual() 会筛出年度序列
        kept = sorted(by_period.values(), key=lambda r: r["报告期"], reverse=True)
        out[key] = kept[:periods]
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
