# -*- coding: utf-8 -*-
"""
检查表分析 - 数据采集与指标计算

用法:
    python3 collect.py 贵州茅台
    python3 collect.py 600519 -o /tmp/mt.json
    python3 collect.py 歌华有线 --periods 16

输出: 单份 JSON，含 元数据 / 行情 / 财报 / 指标 / 排雷信号 / 治理线索
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datasource as ds  # noqa: E402

YI = 1e8  # 亿元


# --------------------------------------------------------------------------
# 取值工具：新浪接口只返回「有值」字段，缺失即 0；字段名存在多套别名
# --------------------------------------------------------------------------
def pick(row: Dict, *aliases, default=None):
    """按候选别名顺序取值，命中第一个非空（非 None/''）即返回。"""
    for a in aliases:
        if a in row and row[a] not in (None, "", "--"):
            return row[a]
    # 退化为包含匹配
    for a in aliases:
        for k in row:
            if a in k and row[k] not in (None, "", "--"):
                return row[k]
    return default


def num(row: Dict, *aliases, default=0.0) -> float:
    """取值并转 float。缺失视为 0（对商誉/借款类科目成立）。"""
    v = pick(row, *aliases, default=None)
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).replace(",", "").replace("%", "").strip()
        if s in ("", "-", "--"):
            return default
        return float(s)
    except ValueError:
        return default


def yi(v: Optional[float], digits=2):
    """
    元 -> 亿元。

    None 保持 None：数据缺失必须能穿透到下游，若在这里退化成 0.0，
    「无数据」会被误读成「该科目为 0」（例如把经营现金流缺失当成现金流为负）。
    """
    if v is None or v == "":
        return None
    try:
        if float(v) == 0:
            return 0.0
        return round(float(v) / YI, digits)
    except (TypeError, ValueError):
        return None


def safe_div(a, b, digits=4, default=None):
    try:
        if b in (None, 0) or a is None:
            return default
        return round(float(a) / float(b), digits)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def pct(x, digits=2):
    if x is None:
        return None
    return round(x * 100, digits)


# --------------------------------------------------------------------------
# 报告期工具
# --------------------------------------------------------------------------
def is_annual(period: str) -> bool:
    return period.endswith("12-31")


def align_periods(income, balance, cashflow, limit=8) -> List[Dict]:
    """把三表按报告期对齐，生成时间序列。"""
    idx = {}
    for src, key in ((income, "income"), (balance, "balance"), (cashflow, "cashflow")):
        for r in src or []:
            p = r.get("报告期")
            if not p:
                continue
            idx.setdefault(p, {})[key] = r
    periods = sorted(idx.keys(), reverse=True)[:limit]
    return [{"报告期": p, **idx[p]} for p in periods]


# --------------------------------------------------------------------------
# 核心指标
# --------------------------------------------------------------------------
def compute_metrics(series: List[Dict], quote: Dict) -> Dict[str, Any]:
    if not series:
        return {}
    latest = series[0]
    inc = latest.get("income", {}) or {}
    bal = latest.get("balance", {}) or {}
    cf = latest.get("cashflow", {}) or {}

    revenue = num(inc, "营业总收入", "营业收入")
    net_profit = num(inc, "归属于母公司所有者的净利润", "净利润")
    # 毛利率：A 股用「营收 - 营业成本」；港股利润表无「营业成本」科目，
    # 但直接披露「毛利润」——优先取披露值，否则退回收入减成本。
    # 若两者都拿不到，必须置 None 而非 0，否则毛利率会被算成 100%。
    gp_disclosed = num(inc, "毛利润", default=None)
    cost = num(inc, "营业成本", default=None)
    if gp_disclosed is not None:
        gross_profit = gp_disclosed
    elif cost is not None:
        gross_profit = revenue - cost
    else:
        gross_profit = None
    invest_income = num(inc, "投资收益")
    fv_change = num(inc, "公允价值变动收益")
    op_profit = num(inc, "营业利润")

    # 现金流缺失（如港股无现金流量表源）时必须返回 None，
    # 不能返回 0——0 会被下游解读成「经营现金流为负」，是严重误判。
    ocf = num(cf, "经营活动产生的现金流量净额", default=None)
    icf = num(cf, "投资活动产生的现金流量净额", default=None)
    fcf = num(cf, "筹资活动产生的现金流量净额", default=None)
    capex = num(cf, "购建固定资产、无形资产和其他长期资产所支付的现金", default=None)

    cash = num(bal, "货币资金")
    tradable = num(bal, "交易性金融资产")
    # 港股特有：短期存款是独立于「货币资金」披露的现金类资产
    short_deposit = num(bal, "短期存款")
    ar = num(bal, "应收账款", "应收票据及应收账款")
    inventory = num(bal, "存货")
    goodwill = num(bal, "商誉")
    contract_liab = num(bal, "合同负债")
    total_assets = num(bal, "资产总计", "资产总计", "负债及股东权益总计")
    total_liab = num(bal, "负债合计", "负债合计", "负债及股东权益总计")
    # 别名差异：制造业多为「归属于母公司股东权益合计」，银行/保险为「归属于母公司股东的权益」
    equity_parent = num(bal, "归属于母公司股东权益合计",
                        "归属于母公司所有者权益合计",
                        "归属于母公司的股东权益合计",
                        "归属于母公司所有者的所有者权益合计",
                        "归属于母公司股东的权益",
                        "归属于母公司所有者权益")

    st_loan = num(bal, "短期借款")
    lt_loan = num(bal, "长期借款")
    bond = num(bal, "应付债券")
    due_1y = num(bal, "一年内到期的非流动负债")
    # 港股融资租赁负债属于有息负债，A 股该科目通常为 0，故可直接相加
    lease = num(bal, "融资租赁负债(流动)", "融资租赁负债(非流动)")
    interest_debt = st_loan + lt_loan + bond + due_1y + lease

    equity_total = num(bal, "所有者权益(或股东权益)合计", "所有者权益合计") or equity_parent

    # 年度序列（用于趋势与 ROE）
    ann = [s for s in series if is_annual(s.get("报告期", ""))]
    rev_hist, np_hist, roe_hist = [], [], []
    for s in ann[:6]:
        i = s.get("income", {}) or {}
        b = s.get("balance", {}) or {}
        c = s.get("cashflow", {}) or {}
        r_ = num(i, "营业总收入", "营业收入")
        n_ = num(i, "归属于母公司所有者的净利润", "净利润")
        e_ = num(b, "归属于母公司股东权益合计", "归属于母公司所有者权益合计",
                 "归属于母公司股东的权益", "归属于母公司所有者权益",
                 "归属于母公司的股东权益合计")
        o_ = num(c, "经营活动产生的现金流量净额", default=None)
        rev_hist.append({"报告期": s["报告期"][:4], "营收亿": yi(r_), "归母净利亿": yi(n_),
                         "OCF亿": yi(o_) if o_ is not None else None,
                         "OCF/净利": safe_div(o_, n_, 3)})
        np_hist.append(yi(n_))
        if e_:
            roe_hist.append({"报告期": s["报告期"][:4], "ROE%": pct(safe_div(n_, e_, 4))})
    rev_hist.reverse()
    roe_hist.reverse()

    # ROE（最新年度，用平均净资产）
    roe = None
    if len(ann) >= 2:
        n0 = num(ann[0].get("income", {}), "归属于母公司所有者的净利润", "净利润")
        e0 = num(ann[0].get("balance", {}), "归属于母公司股东权益合计",
                 "归属于母公司所有者权益合计", "归属于母公司股东的权益",
                 "归属于母公司的股东权益合计")
        e1 = num(ann[1].get("balance", {}), "归属于母公司股东权益合计",
                 "归属于母公司所有者权益合计", "归属于母公司股东的权益",
                 "归属于母公司的股东权益合计")
        if e0 and e1:
            roe = safe_div(n0, (e0 + e1) / 2, 4)
    elif ann:
        n0 = num(ann[0].get("income", {}), "归属于母公司所有者的净利润", "净利润")
        e0 = num(ann[0].get("balance", {}), "归属于母公司股东权益合计",
                 "归属于母公司所有者权益合计", "归属于母公司股东的权益",
                 "归属于母公司的股东权益合计")
        roe = safe_div(n0, e0, 4)

    # ROIC 近似：EBIT / (有息负债 + 所有者权益)
    ebit = op_profit if op_profit else net_profit
    roic = safe_div(ebit, (interest_debt + equity_total), 4) if (interest_debt + equity_total) else None

    # 股息率 / 派息率（每股口径，见 build_per_share）
    price = quote.get("price") or 0
    ps = quote.get("_per_share") or {}
    ttm_dps = ps.get("ttm_dps")
    eps = ps.get("eps")
    div_yield = safe_div(ttm_dps, price, 4) if (ttm_dps and price) else None
    payout = ps.get("latest_year_payout")

    return {
        "最新报告期": latest.get("报告期"),
        "营收亿": yi(revenue),
        "归母净利亿": yi(net_profit),
        "扣非净利亿": yi(num(inc, "扣除非经常性损益后的净利润")),
        "毛利率%": pct(safe_div(gross_profit, revenue)),
        "净利率%": pct(safe_div(net_profit, revenue)),
        "ROE%": pct(roe),
        "ROIC%": pct(roic),
        "资产负债率%": pct(safe_div(total_liab, total_assets)),
        "OCF亿": yi(ocf),
        "OCF/净利": safe_div(ocf, net_profit, 3),
        "自由现金流亿": yi(ocf - capex) if (ocf is not None and capex is not None) else None,
        "投资净流亿": yi(icf),
        "筹资净流亿": yi(fcf),
        "货币资金亿": yi(cash),
        "交易性金融资产亿": yi(tradable),
        "有息负债亿": yi(interest_debt),
        # 港股把存款单列为「短期存款」（腾讯 2025 年报该项 2143 亿），
        # 不计入会严重低估现金、把净现金算成大额负值
        "净现金亿": yi(cash + tradable + short_deposit - interest_debt),
        "商誉亿": yi(goodwill),
        "商誉/净资产%": pct(safe_div(goodwill, equity_parent)) if equity_parent else 0.0,
        "合同负债亿": yi(contract_liab),
        "应收账款亿": yi(ar),
        "存货亿": yi(inventory),
        "总资产亿": yi(total_assets),
        "归母权益亿": yi(equity_parent),
        "投资收益亿": yi(invest_income),
        "投资收益/营业利润%": pct(safe_div(invest_income + fv_change, op_profit)) if op_profit else None,
        "主业经营性利润亿": yi(op_profit - invest_income - fv_change) if op_profit else None,
        "股息率%": pct(div_yield),
        "派息率%": pct(payout),
        "营收趋势": rev_hist,
        "ROE趋势": roe_hist,
        "净利趋势亿": np_hist[::-1],
    }


# --------------------------------------------------------------------------
# 排雷信号（纯规则，二元触发）
# --------------------------------------------------------------------------
RAISE_KEYWORDS = ("募集资金", "非公开发行", "定向增发", "募投", "闲置募集资金",
                  "募集资金存放", "前次募集资金")
PENALTY_KEYWORDS = ("处罚", "警示函", "监管措施", "立案", "违规", "纪律处分",
                    "监管关注", "监管工作函", "问询函")
PLEDGE_KEYWORDS = ("质押", "冻结")
REDUCE_KEYWORDS = ("减持", "套现")
BUYBACK_KEYWORDS = ("回购", "增持")
COMMIT_KEYWORDS = ("业绩承诺", "业绩补偿", "盈利预测实现", "承诺完成")


def build_per_share(dividends: List[Dict], series: List[Dict],
                    quote: Dict) -> Dict[str, Any]:
    """
    构建每股口径：TTM 股息、分年度派息、EPS、派息率。

    关键点：
      - 很多公司一年多次分红（中期 + 年度），只取单笔会严重低估股息率
      - 股息率用「近 12 个月除权的分红合计 / 当前股价」
      - 派息率用「最近完整年度的分红合计 / 该年度 EPS」
    """
    ps: Dict[str, Any] = {"ttm_dps": None, "eps": None, "year_dps": {},
                          "latest_year": None, "latest_year_payout": None}

    if not dividends:
        return ps

    from datetime import datetime as _dt, timedelta
    # TTM 锚点取「今天」与「最新除权日」中较晚者：
    # 既符合近12个月定义，又避免分红数据滞后时漏掉最新一笔
    latest_ex = max((d.get("date") or "" for d in dividends), default="")
    try:
        anchor = max(_dt.strptime(latest_ex, "%Y-%m-%d"), _dt.now())
    except ValueError:
        anchor = _dt.now()
    cutoff = (anchor - timedelta(days=365)).strftime("%Y-%m-%d")

    ttm = 0.0
    year_dps: Dict[str, float] = {}
    for d in dividends:
        dps = d.get("dps") or 0
        if not dps:
            continue
        # 严格大于 cutoff，避免恰好 365 天的上一笔被重复计入
        if (d.get("date") or "") > cutoff:
            ttm += dps
        y = (d.get("report_date") or "")[:4]
        if y:
            year_dps[y] = round(year_dps.get(y, 0.0) + dps, 6)
    ps["ttm_dps"] = round(ttm, 4) if ttm else None
    ps["year_dps"] = year_dps

    # EPS：取最近年度报告期
    ann = [s for s in series if is_annual(s.get("报告期", ""))]
    if ann:
        inc = ann[0].get("income", {}) or {}
        ps["eps"] = num(inc, "基本每股收益") or None
        y = ann[0]["报告期"][:4]
        ps["latest_year"] = y
        if ps["eps"] and year_dps.get(y):
            ps["latest_year_payout"] = safe_div(year_dps[y], ps["eps"], 4)
    return ps


def detect_signals(metrics: Dict, anns: List[Dict], dividends: List[Dict],
                   quote: Dict) -> List[Dict]:
    signals: List[Dict] = []
    # 金融股等不适用口径下被显式置空的指标，相关信号一律跳过，避免误报
    disabled = set(metrics.get("不适用指标") or [])

    def add(sid, name, level, detail, value=None, basis=""):
        signals.append({"id": sid, "signal": name, "level": level,
                        "detail": detail, "value": value, "basis": basis})

    # --- 财务类 ---
    gw = metrics.get("商誉/净资产%")
    if gw is not None and gw >= 20:
        add("S1", "商誉占净资产过高", "high",
            f"商誉/归母净资产 = {gw}%，减值风险显著", gw, "阈值 ≥20%")
    elif gw is not None and gw >= 10:
        add("S1", "商誉占净资产偏高", "mid", f"商誉/归母净资产 = {gw}%", gw, "阈值 ≥10%")

    ocf_ratios = ([r.get("OCF/净利") for r in metrics.get("营收趋势", [])
                   if r.get("OCF/净利") is not None]
                  if "OCF/净利" not in disabled else [])
    weak = [r for r in ocf_ratios[-3:] if r < 1]
    if len(weak) >= 2:
        add("S2", "盈利含金量不足", "high",
            f"近 {len(ocf_ratios)} 年中有 {len(weak)} 年 OCF/净利 < 1：{ocf_ratios}",
            ocf_ratios, "连续2年 OCF/净利<1")

    iip = metrics.get("投资收益/营业利润%")
    if iip is not None and iip >= 50:
        add("S3", "利润结构失真（靠投资）", "high",
            f"投资收益+公允价值变动 占营业利润 {iip}%，主业经营性利润仅 "
            f"{metrics.get('主业经营性利润亿')} 亿", iip, "阈值 ≥50%")

    np_trend = metrics.get("净利趋势亿", [])
    if len(np_trend) >= 3 and all(np_trend[i] < np_trend[i - 1] for i in range(1, min(3, len(np_trend)))):
        add("S4", "净利连续下滑", "mid", f"近三年归母净利：{np_trend[:3]} 亿元，逐年走低",
            np_trend[:3], "连续2年下滑")

    dy = metrics.get("股息率%")
    nc = metrics.get("净现金亿")
    has_buyback = any(k in a.get("title", "") for a in anns for k in BUYBACK_KEYWORDS)
    if dy is not None and dy < 1 and nc and nc > 5 and not has_buyback:
        add("S5", "股东回报缺失", "mid",
            f"股息率仅 {dy}%，账上净现金 {nc} 亿元，但无回购动作", dy,
            "股息率<1% 且 净现金>5亿 且 无回购")

    dar = metrics.get("资产负债率%")
    if dar is not None and dar >= 70:
        add("S6", "高杠杆", "mid", f"资产负债率 {dar}%", dar, "阈值 ≥70%")

    # --- 治理线索类（来自公告标题，需人工/LLM 复核）---
    raise_anns = [a for a in anns if any(k in a["title"] for k in RAISE_KEYWORDS)]
    if raise_anns:
        add("G1", "存在募资相关公告（需核查闲置率）", "review",
            f"近 {len(raise_anns)} 份募资相关公告，须下载专项报告核对投入进度",
            [f'{a["date"]} {a["title"][:40]}' for a in raise_anns[:5]],
            "公告标题关键词命中")

    penalty_anns = [a for a in anns if any(k in a["title"] for k in PENALTY_KEYWORDS)]
    if penalty_anns:
        add("G2", "监管/处罚线索（需复核性质）", "review",
            f"命中 {len(penalty_anns)} 份公告；注意区分『问询函/监管工作函』与『行政处罚』",
            [f'{a["date"]} {a["title"][:40]}' for a in penalty_anns[:5]],
            "公告标题关键词命中")

    reduce_anns = [a for a in anns if any(k in a["title"] for k in REDUCE_KEYWORDS)]
    if reduce_anns:
        add("G3", "股东减持线索", "review",
            f"命中 {len(reduce_anns)} 份减持相关公告",
            [f'{a["date"]} {a["title"][:40]}' for a in reduce_anns[:5]],
            "公告标题关键词命中")

    if has_buyback:
        add("G4", "存在回购/增持动作（正面）", "positive",
            "检索到回购或大股东增持公告，需核实是否注销",
            [f'{a["date"]} {a["title"][:40]}' for a in anns
             if any(k in a["title"] for k in BUYBACK_KEYWORDS)][:5],
            "公告标题关键词命中")

    commit_anns = [a for a in anns if any(k in a["title"] for k in COMMIT_KEYWORDS)]
    if commit_anns:
        add("G5", "存在业绩承诺事项（需核查达成）", "review",
            f"命中 {len(commit_anns)} 份业绩承诺相关公告",
            [f'{a["date"]} {a["title"][:40]}' for a in commit_anns[:5]],
            "公告标题关键词命中")

    # 分红连续性
    recent_div = [d for d in dividends if d.get("bonus_rmb")]
    if not recent_div:
        add("D1", "无分红记录", "mid", "未检索到分红派息记录", None, "分红表为空")
    elif len(recent_div) >= 5:
        add("D2", "分红连续", "positive", f"近 {len(recent_div)} 期存在分红派息",
            len(recent_div), "分红记录数≥5")

    return signals


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def collect(query: str, periods: int = 12, with_anns: bool = True) -> Dict[str, Any]:
    started = datetime.now()
    sym = ds.resolve_symbol(query)
    if not sym:
        return {"ok": False, "error": f"无法解析证券：{query}"}

    market, code = sym["market"], sym["code"]
    secucode = ds.to_secucode(market, code)
    is_hk = market == "hk"
    result: Dict[str, Any] = {
        "ok": True,
        "meta": {
            "query": query, "name": sym.get("name", ""), "code": code,
            "market": market, "market_name": ds.MARKET_NAME.get(market, market),
            "secucode": secucode, "currency": "HKD" if is_hk else "CNY",
            "fetch_time": started.strftime("%Y-%m-%d %H:%M:%S"),
            "warnings": [],
        },
    }

    # 1. 行情
    try:
        q = ds.quote([ds._tencent_prefix(market, code)])
        result["quote"] = q.get(code.zfill(5) if is_hk else code, {})
    except Exception as e:  # noqa: BLE001
        result["quote"] = {}
        result["meta"]["warnings"].append(f"行情获取失败: {e}")

    # 2. 财报
    #    A 股：主源新浪（字段全），失败降级东财 DMSK
    #    港股：东财 HKF10 专用源（纵表 + 中国香港准则科目，已在 datasource 层转置对齐）
    if is_hk:
        st = ds.hk_statements(secucode, periods=periods)
        if not any(st.values()):
            result["meta"]["warnings"].append(
                "港股财报源不可用（东财 HKF10 未覆盖该标的），仅提供行情与估值")
    else:
        st = ds.sina_statements(market, code, periods=periods)
        if not any(st.values()):
            result["meta"]["warnings"].append("新浪财报源不可用，尝试东财 DMSK 备源")
            st = ds.em_statements(secucode, periods=periods)
        if not any(st.values()):
            result["meta"]["warnings"].append(
                "财报数据不可用，仅提供行情与估值")
    result["statements"] = st

    # 2b. 港股 PB 回填：行情源无可靠 PB 字段，用归母净资产反推
    if is_hk and result["quote"].get("price"):
        latest_bal = None
        for row in (st.get("balance") or []):
            latest_bal = row
            break
        if latest_bal:
            eq = num(latest_bal, "归属于母公司股东权益合计", default=None)
            pb = ds.hk_pb_from_equity(code, eq, result["quote"]["price"])
            if pb:
                result["quote"]["pb"] = pb

    series = align_periods(st.get("income"), st.get("balance"), st.get("cashflow"),
                           limit=periods)
    result["series"] = series

    # 3. 分红 / 股东户数 / 公告（仅 A 股可靠）
    if not is_hk:
        result["dividends"] = ds.dividend_history(code)
        result["holders"] = ds.holder_num(code)
        # 标记分红源可用：下游据此区分「确认无分红」与「数据缺失」
        result["meta"]["_dividend_source"] = True
    else:
        result["dividends"] = []
        result["holders"] = []
        result["meta"]["_dividend_source"] = False
        result["meta"]["warnings"].append(
            "【港股数据边界】分红与股东户数暂无免登录公开源，股息率/派息率/资本配置中的"
            "分红项按缺失处理（中性计分，不计 0 分），估值维度因此偏保守")

    if is_hk:
        # 巨潮只覆盖 A 股，港股无免登录公告源
        result["announcements"] = []
        result["meta"]["_announcement_source"] = False
    else:
        result["announcements"] = ds.announcements(code) if with_anns else []
        result["meta"]["_announcement_source"] = bool(with_anns)

    # 4. 每股口径（股息率/派息率）
    result["per_share"] = build_per_share(result["dividends"], series, result["quote"])
    result["quote"]["_per_share"] = result["per_share"]

    # 5. 字段补全：新浪利润表无「扣非净利」，用东财 DMSK 补齐；顺带取行业名
    try:
        if series and not is_hk:
            em_inc = ds.em_statements(secucode, periods=min(periods, 12)).get("income", [])
            ded = {}
            for r in em_inc:
                p = str(r.get("REPORT_DATE", ""))[:10]
                if p and r.get("DEDUCT_PARENT_NETPROFIT") is not None:
                    ded[p] = r["DEDUCT_PARENT_NETPROFIT"]
                if r.get("INDUSTRY_NAME") and not result["meta"].get("industry"):
                    result["meta"]["industry"] = r["INDUSTRY_NAME"]
            hit = 0
            for s in series:
                v = ded.get(s.get("报告期"))
                if v is not None and "income" in s:
                    s["income"]["扣除非经常性损益后的净利润"] = v
                    hit += 1
            if hit:
                result["meta"]["deduct_filled"] = hit
    except Exception as e:  # noqa: BLE001
        result["meta"]["warnings"].append(f"扣非净利补全失败: {e}")

    # 6. 指标 + 信号
    result["metrics"] = compute_metrics(series, result["quote"])

    # 7. 行业适用性判定
    #    银行/保险/证券的财报结构与实业根本不同：货币资金含客户存款、高杠杆是经营常态、
    #    经营现金流无实业参考意义。直接套实业口径会得出荒谬结论，必须显式标注并禁用。
    industry = result["meta"].get("industry") or ""
    is_financial = any(k in industry for k in ("银行", "保险", "证券", "多元金融"))
    result["meta"]["is_financial"] = is_financial
    if is_financial:
        result["meta"]["warnings"].append(
            f"【行业适用性】{industry}属于金融行业：货币资金含客户存款、高杠杆为经营常态、"
            f"经营现金流不具实业参考意义。净现金/有息负债/资产负债率/OCF含金量 已禁用，"
            f"本模型评分仅供粗略参考，建议改用金融股专用框架"
            f"（不良率、拨备覆盖率、资本充足率 / 偿付能力充足率）复核。")
        for k in ("净现金亿", "有息负债亿", "资产负债率%", "OCF/净利",
                  "自由现金流亿", "投资收益/营业利润%", "主业经营性利润亿",
                  "毛利率%", "净利率%"):
            result["metrics"][k] = None
        result["metrics"]["不适用指标"] = [
            "净现金亿", "有息负债亿", "资产负债率%", "OCF/净利",
            "自由现金流亿", "投资收益/营业利润%", "主业经营性利润亿",
            "毛利率%", "净利率%"]

    result["signals"] = detect_signals(
        result["metrics"], result["announcements"], result["dividends"], result["quote"])

    # 8. 港股支持度标注
    if is_hk:
        # 财报已接入，但现金流量表无源——OCF 类指标必须显式标缺失，
        # 绝不能让「无数据」被误读成「经营现金流为负」。
        if not (st.get("cashflow") or []):
            result["meta"]["partial_support"] = True
            result["meta"]["warnings"].append(
                "【港股数据边界】现金流量表暂无免登录公开源，"
                "OCF/净利、自由现金流等指标按缺失处理（中性计分，非 0 分）。")
        if not series:
            result["meta"]["partial_support"] = True
            result["meta"]["warnings"].append(
                "【港股支持度】未能获取该标的财报（东财 HKF10 未覆盖），"
                "仅提供行情与估值，财务/管理层维度无法评估。")

    result["meta"]["elapsed_sec"] = round((datetime.now() - started).total_seconds(), 1)
    result["meta"]["data_completeness"] = {
        "行情": bool(result["quote"].get("price")),
        "财报三表": bool(series),
        "分红": bool(result["dividends"]),
        "股东户数": bool(result["holders"]),
        "公告": bool(result["announcements"]),
    }
    return result


def main():
    ap = argparse.ArgumentParser(description="检查表分析 - 数据采集与指标计算")
    ap.add_argument("query", help="股票名称 / 代码 / 拼音，如 贵州茅台、600519、gzmt")
    ap.add_argument("-o", "--out", help="输出 JSON 路径")
    ap.add_argument("--periods", type=int, default=12, help="财报期数，默认 12")
    ap.add_argument("--no-anns", action="store_true", help="跳过公告抓取（更快）")
    ap.add_argument("--brief", action="store_true", help="终端只打印摘要")
    args = ap.parse_args()

    data = collect(args.query, periods=args.periods, with_anns=not args.no_anns)

    out = args.out
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    if args.brief:
        brief = {k: data.get(k) for k in ("meta", "quote", "metrics", "signals")}
        print(json.dumps(brief, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
    if out:
        print(f"\n[已保存] {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
