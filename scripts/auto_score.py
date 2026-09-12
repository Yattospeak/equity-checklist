# -*- coding: utf-8 -*-
"""
检查表分析 - 规则化自动打分（基准分）

定位：让「输入代码 → 出报告」在无模型介入时也能跑通。

诚实边界（重要）：
  - 财务 / 估值 维度：纯数据驱动，自动分可信度高
  - 业务 维度：以 ROE/毛利率/成长性作代理，只能算粗略
  - 管理层 维度：仅「资本配置」可部分量化；诚信度、经营能力、内部人持股
    必须联网核查公告/处罚/减持，脚本 **无法替代**，
    一律给中性 3.0 并标记 source=auto，报告中明示需人工复核。

规则（检查表评估模型 v2.1 / v2.2）：
  - 规则A：管理层任一子维度 ≤2 → 标注「⚠️ 管理层风险」+ 下调置信度
  - 规则B：并购暴雷 + 再融资圈钱 + 监管处罚 三瑕疵同现 → 管理层封顶 2.5 且一票否决
  - 硬性闸门：任一维度 <3.0 → 结论降级为「拒绝/回避」

用法:
    python3 auto_score.py metrics.json -o scores.json
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WEIGHTS = {"business": 0.30, "management": 0.25, "financial": 0.25, "valuation": 0.20}


def clamp(v, lo=1.0, hi=5.0):
    return max(lo, min(hi, round(v, 2)))


def annualized(base, curr, years: int = 2):
    """
    线性年化增速。相比几何 CAGR 的优势：
    亏损年份（base 或 curr 为负）不会产生复数，也不会出现无意义的增速。
    """
    try:
        if base is None or curr is None or base == 0 or years <= 0:
            return None
        return (float(curr) / float(base) - 1) / years
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def avg(xs: List[float]) -> float:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 3.0


# --------------------------------------------------------------------------
# 财务维度（v2.2 锚点表，数据驱动，可信）
# --------------------------------------------------------------------------
def score_financial(m: Dict) -> Dict[str, Any]:
    ocf = m.get("OCF/净利")
    dar = m.get("资产负债率%")
    net_cash = m.get("净现金亿")
    gw = m.get("商誉/净资产%")
    roe = m.get("ROE%")
    iip = m.get("投资收益/营业利润%")

    # 1) 盈利含金量 OCF/净利
    if ocf is None:
        s_ocf = 3.0
    elif ocf >= 1.2:
        s_ocf = 5.0
    elif ocf >= 1.0:
        s_ocf = 4.2
    elif ocf >= 0.8:
        s_ocf = 3.2
    elif ocf >= 0.5:
        s_ocf = 2.2
    else:
        s_ocf = 1.2

    # 2) 资本结构
    if dar is None:
        s_bs = 3.0
    elif net_cash is not None and net_cash > 0 and dar < 40:
        s_bs = 5.0
    elif dar < 40:
        s_bs = 4.2
    elif dar < 55:
        s_bs = 3.5
    elif dar < 70:
        s_bs = 2.5
    else:
        s_bs = 1.5

    # 3) 雷 / 盈利质量
    #    注意：ROE 缺失时回退中性 3.0 并标注，绝不能落进「最差档」——
    #    否则金融股等口径差异的标的会被误判为财务爆雷。
    roe_missing = roe is None
    if iip is not None and iip >= 50:
        s_land = 2.0          # 利润靠投资，主业含金量低
    elif gw is not None and gw >= 20:
        s_land = 2.0          # 商誉悬顶
    elif gw is not None and gw >= 10:
        s_land = 3.0
    elif roe is None:
        s_land = 3.0
    elif roe >= 20:
        s_land = 5.0
    elif roe >= 12:
        s_land = 4.2
    elif roe >= 8:
        s_land = 3.5
    elif roe >= 3:
        s_land = 2.5
    else:
        s_land = 1.5

    score = clamp(avg([s_ocf, s_bs, s_land]))
    flags = []
    if ocf is None:
        flags.append("OCF/净利 缺失→中性")
    if dar is None:
        flags.append("资产负债率 缺失/不适用→中性")
    if roe_missing:
        flags.append("ROE 缺失→中性（未作最差处理）")
    return {
        "score": score, "source": "auto",
        "note": f"盈利含金量 {s_ocf:.1f} / 资本结构 {s_bs:.1f} / 盈利与雷 {s_land:.1f}；"
                f"OCF/净利 {ocf}, 资产负债率 {dar}%, ROE {roe}%, 商誉/净资产 {gw}%"
                + (f"；⚠ {'，'.join(flags)}" if flags else ""),
        "detail": {"含金量": s_ocf, "资本结构": s_bs, "盈利与雷": s_land},
    }


# --------------------------------------------------------------------------
# 估值维度（保持灵活综合，脚本给初步判断）
# --------------------------------------------------------------------------
def score_valuation(q: Dict, m: Dict) -> Dict[str, Any]:
    pe = q.get("pe_ttm")
    pb = q.get("pb")
    dy = m.get("股息率%")
    notes, parts = [], []

    # PE 绝对水平（需人工用历史分位校准）
    if pe is None or pe <= 0:
        s_pe = None
        notes.append("PE 无效（亏损或无数据）")
    elif pe < 10:
        s_pe, t = 5.0, "PE<10 显著低估"
    elif pe < 16:
        s_pe, t = 4.3, "PE 10-16 偏低"
    elif pe < 22:
        s_pe, t = 3.5, "PE 16-22 中性"
    elif pe < 30:
        s_pe, t = 2.8, "PE 22-30 偏高"
    elif pe < 45:
        s_pe, t = 2.0, "PE 30-45 高"
    else:
        s_pe, t = 1.3, "PE>45 极高或盈利失真"
    if s_pe:
        parts.append(s_pe)
        notes.append(f"PE(TTM)={pe} → {t}")

    # PB
    if pb is not None and pb > 0:
        if pb < 1:
            s_pb, t = 4.0, "PB<1 破净（需区分陷阱与错杀）"
        elif pb < 2:
            s_pb, t = 4.2, "PB 1-2 合理"
        elif pb < 4:
            s_pb, t = 3.4, "PB 2-4 中性"
        elif pb < 8:
            s_pb, t = 2.6, "PB 4-8 偏贵"
        else:
            s_pb, t = 1.8, "PB>8 很贵"
        parts.append(s_pb)
        notes.append(f"PB={pb} → {t}")

    # 股息率（安全垫）
    if dy is not None:
        if dy >= 5:
            s_dy, t = 4.8, f"股息率 {dy}% 安全垫厚"
        elif dy >= 3:
            s_dy, t = 4.2, f"股息率 {dy}% 良好"
        elif dy >= 1.5:
            s_dy, t = 3.5, f"股息率 {dy}% 一般"
        elif dy > 0:
            s_dy, t = 2.8, f"股息率 {dy}% 偏低"
        else:
            s_dy, t = 2.0, "无股息"
        parts.append(s_dy)
        notes.append(t)

    score = clamp(avg(parts)) if parts else 3.0
    return {
        "score": score, "source": "auto",
        "note": "；".join(notes) + "。⚠ 未接入历史估值分位，建议用近10年分位人工校准",
    }


# --------------------------------------------------------------------------
# 业务维度（代理指标，粗略）
# --------------------------------------------------------------------------
def score_business(m: Dict) -> Dict[str, Any]:
    roe = m.get("ROE%")
    gm = m.get("毛利率%")
    hist = m.get("营收趋势") or []
    revs = [h.get("营收亿") for h in hist if h.get("营收亿") is not None]
    nps = [h.get("归母净利亿") for h in hist if h.get("归母净利亿") is not None]
    notes = []

    if roe is None:
        s_roe = None
    elif roe >= 20:
        s_roe = 5.0
    elif roe >= 15:
        s_roe = 4.3
    elif roe >= 10:
        s_roe = 3.6
    elif roe >= 5:
        s_roe = 2.6
    else:
        s_roe = 1.5
    if s_roe:
        notes.append(f"ROE {roe}% → 护城河代理 {s_roe}")

    s_gm = None
    if gm is not None:
        if gm >= 60:
            s_gm = 5.0
        elif gm >= 40:
            s_gm = 4.3
        elif gm >= 25:
            s_gm = 3.5
        elif gm >= 15:
            s_gm = 2.6
        else:
            s_gm = 1.6
        notes.append(f"毛利率 {gm}% → 定价权代理 {s_gm}")

    s_growth = None
    if len(revs) >= 3:
        # 用线性年化而非几何 CAGR：亏损年份底数为负会开方出复数
        yoy = annualized(revs[-3], revs[-1], 2)
        npg = annualized(nps[-3], nps[-1], 2) if len(nps) >= 3 else yoy
        g = (yoy + npg) / 2 if (yoy is not None and npg is not None) else (yoy if yoy is not None else npg)
        if g >= 0.20:
            s_growth = 5.0
        elif g >= 0.10:
            s_growth = 4.2
        elif g >= 0.03:
            s_growth = 3.4
        elif g >= -0.05:
            s_growth = 2.4
        else:
            s_growth = 1.5
        notes.append(f"近2年营收/净利复合增速 ≈ {g*100:.1f}% → 成长代理 {s_growth}")

    score = clamp(avg([s_roe, s_gm, s_growth]))
    return {
        "score": score, "source": "auto",
        "note": "；".join(notes) + "。⚠ 业务维度以 ROE/毛利率/增速为代理，护城河判断需行业认知复核",
    }


# --------------------------------------------------------------------------
# 管理层维度（部分可量化，诚信度必须人工核查）
# --------------------------------------------------------------------------
def score_management(d: Dict) -> Dict[str, Any]:
    m = d.get("metrics") or {}
    q = d.get("quote", {})
    anns = d.get("announcements") or []
    divs = d.get("dividends") or []
    titles = " ".join(a.get("title", "") for a in anns)

    # --- 资本配置记分卡（可量化部分，满分 30 → 归一到 5）---
    card, card_notes = 0, []

    # 1) 分红（0-6）
    dy = m.get("股息率%")
    payout = m.get("派息率%")
    if dy is None or not divs:
        card_notes.append("无分红 0/6")
    else:
        if dy >= 4:
            c = 6
        elif dy >= 2.5:
            c = 5
        elif dy >= 1.5:
            c = 4
        elif dy >= 0.8:
            c = 3
        else:
            c = 1
        card += c
        card_notes.append(f"分红：股息率 {dy}%（派息率 {payout}%）{c}/6")

    # 2) 回购/增持（0-6）
    has_buyback = ("回购" in titles)
    has_increase = ("增持" in titles)
    if has_buyback:
        card += 6
        card_notes.append("存在回购公告 6/6（需核实是否注销）")
    elif has_increase:
        card += 4
        card_notes.append("存在大股东增持 4/6")
    else:
        card_notes.append("未见回购/增持 0/6")

    # 3) 再融资克制（0-6）：有定增/可转债公告扣分
    raise_kw = ("非公开发行" in titles, "定增" in titles, "可转债" in titles,
                "配股" in titles, "募集资金" in titles)
    if any(raise_kw):
        card_notes.append("存在再融资/募资公告 0/6（须核查募资使用进度）")
    else:
        card += 6
        card_notes.append("未见再融资 6/6")

    # 4) 并购与商誉（0-6）
    gw = m.get("商誉/净资产%") or 0
    if gw >= 20:
        card_notes.append(f"商誉/净资产 {gw}% 偏高 1/6")
        card += 1
    elif gw >= 10:
        card_notes.append(f"商誉/净资产 {gw}% 需关注 3/6")
        card += 3
    else:
        card += 6
        card_notes.append(f"商誉占比 {gw}% 低 6/6")

    # 5) 资本开支纪律（0-6）：OCF 能否覆盖 capex
    ocf = m.get("OCF亿")
    fcf = m.get("自由现金流亿")
    if ocf is None:
        card_notes.append("现金流数据缺失 0/6")
    else:
        if (fcf or 0) > 0:
            card += 6
            card_notes.append(f"自由现金流 {fcf} 亿为正 6/6")
        elif ocf > 0:
            card += 3
            card_notes.append(f"OCF {ocf} 亿为正但不足以覆盖资本开支 3/6")
        else:
            card_notes.append(f"经营现金流为负 {ocf} 亿 0/6")

    capital_alloc = clamp(card / 30 * 5, 1.0, 5.0)

    # --- 不可自动核查项：给中性分并明确标注 ---
    integrity = 3.0
    competence = clamp(avg([_roe_proxy(m)]), 1.0, 5.0) if m.get("ROE%") is not None else 3.0
    ownership = 3.0
    compensation = 3.0

    checks = [
        {"item": "历史业绩承诺兑现", "result": "需检索重组/收购标的承诺实现公告", "flag": False},
        {"item": "并购暴雷（高溢价+商誉减值）",
         "result": f"商誉 {m.get('商誉亿')} 亿，占净资产 {m.get('商誉/净资产%')}%",
         "flag": bool(gw and gw >= 20)},
        {"item": "再融资圈钱（募资长期闲置）",
         "result": "需下载募集资金专项报告核对投入进度"
                   if any(raise_kw) else "近期无再融资公告",
         "flag": any(raise_kw)},
        {"item": "监管处罚 / 纪律处分",
         "result": "需核查证监会、交易所处罚记录（区别于问询函）", "flag": False},
        {"item": "实控人/大股东减持",
         "result": "需核查减持公告与权益变动书",
         "flag": "减持" in titles},
    ]

    mg_score = clamp(avg([integrity, competence, capital_alloc, ownership, compensation]))

    # 规则 A：任一子维度 ≤2
    subs = {"诚信度": integrity, "经营能力": competence, "资本配置": capital_alloc,
            "内部人持股": ownership, "薪酬对齐": compensation}
    triggered_a = [k for k, v in subs.items() if v <= 2.0]

    return {
        "score": mg_score, "source": "auto",
        "note": f"资本配置记分卡 {card}/30 → {capital_alloc:.1f}；"
                f"诚信度/内部人持股/薪酬对齐 **未联网核查，按中性 3.0 计**，须人工复核。"
                f"明细：{' | '.join(card_notes)}",
        "detail": subs,
        "checks": checks,
        "rule_a_triggered": triggered_a,
    }


def _roe_proxy(m: Dict) -> float:
    roe = m.get("ROE%")
    if roe is None:
        return 3.0
    if roe >= 20:
        return 4.5
    if roe >= 15:
        return 4.0
    if roe >= 10:
        return 3.5
    if roe >= 5:
        return 2.7
    return 1.8


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def auto_score(d: Dict) -> Dict[str, Any]:
    m = d.get("metrics") or {}
    q = d.get("quote", {})

    # 数据不足保护：核心财务缺失时拒绝给出综合评分。
    # 宁可输出「数据不足」，也不能在数据残缺的情况下给出看似精确的错误结论。
    if not m or (m.get("营收亿") in (None, 0) and m.get("归母净利亿") is None):
        return {
            "total": None, "decision": "数据不足", "confidence": "—",
            "insufficient_data": True,
            "risk_flag": "核心财务数据缺失，无法完成四维评估",
            "dimensions": {},
            "summary": "未能获取该标的的财务数据，因此不给出综合评分。"
                       "以下仅展示可获取的行情与估值信息。",
            "disclaimer": "数据不足时不作评分，是刻意为之：残缺数据得出的综合分"
                          "比没有分数更具误导性。请补充数据源后重试。",
        }

    fin = score_financial(m)
    val = score_valuation(q, m)
    bus = score_business(m)
    mg = score_management(d)

    dims = {"business": bus, "management": mg, "financial": fin, "valuation": val}
    total = round(sum(dims[k]["score"] * w for k, w in WEIGHTS.items()), 2)

    # 规则 B：三瑕疵同现（脚本层面无法确认，仅留接口）
    # 规则 A：管理层子维度 ≤2
    risk_flag = None
    confidence = "中"
    triggered_a = mg.get("rule_a_triggered") or []
    if triggered_a:
        risk_flag = f"管理层风险：{'、'.join(triggered_a)} 得分 ≤2（规则A）"
        confidence = "低"

    # 硬性闸门：任一维度 <3.0
    low = [k for k, v in dims.items() if v["score"] < 3.0]
    if low:
        risk_flag = (risk_flag + "；" if risk_flag else "") + \
                    "触发硬性闸门（维度 <3.0）：" + "、".join(
                        dict(zip(WEIGHTS, ["业务", "管理层", "财务", "估值"])).get(k, k) for k in low)

    decision = decision_text(total, low)
    if triggered_a or low:
        confidence = "低"

    formula = " + ".join(
        f"{dims[k]['score']:.2f}×{int(w*100)}%" for k, w in WEIGHTS.items()
    ) + f" = {total:.2f}"

    return {
        "total": total,
        "decision": decision,
        "confidence": confidence,
        "risk_flag": risk_flag,
        "formula": formula,
        "dimensions": {
            "business": {"score": bus["score"], "note": bus["note"], "source": "auto"},
            "management": {"score": mg["score"], "note": mg["note"], "source": "auto"},
            "financial": {"score": fin["score"], "note": fin["note"], "source": "auto"},
            "valuation": {"score": val["score"], "note": val["note"], "source": "auto"},
        },
        "management_detail": {
            "integrity": mg["detail"]["诚信度"],
            "competence": mg["detail"]["经营能力"],
            "capital_allocation": mg["detail"]["资本配置"],
            "ownership": mg["detail"]["内部人持股"],
            "compensation": mg["detail"]["薪酬对齐"],
            "note": mg["note"],
            "checks": mg["checks"],
        },
        "auto": True,
        "disclaimer": "本报告由规则引擎自动生成。业务与管理层维度中的诚信度、内部人持股、"
                      "薪酬对齐未联网核查（按中性 3.0 计），结论仅供研究参考，"
                      "正式决策前须完成管理层瑕疵人工核查。",
    }


def decision_text(total: float, low: List[str]) -> str:
    if low:
        return "回避" if total >= 2.0 else "拒绝"
    if total >= 4.2:
        return "强烈买入"
    if total >= 3.8:
        return "买入"
    if total >= 3.4:
        return "买入谨慎"
    if total >= 3.0:
        return "谨慎"
    if total >= 2.5:
        return "中性"
    return "回避"


def main():
    ap = argparse.ArgumentParser(description="检查表分析 - 规则化自动打分")
    ap.add_argument("metrics", help="collect.py 输出的 JSON")
    ap.add_argument("-o", "--out", help="输出 scores JSON")
    args = ap.parse_args()

    with open(args.metrics, encoding="utf-8") as f:
        data = json.load(f)
    scores = auto_score(data)
    text = json.dumps(scores, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[已保存] {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
