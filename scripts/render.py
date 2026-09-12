# -*- coding: utf-8 -*-
"""
检查表分析 - HTML 报告渲染

设计约束（面向微信内置浏览器 / 小程序 webview）：
  1. 单文件产出：CSS 全部内联，零外部依赖（CDN 在部分网络下不可用）
  2. 移动端优先：viewport + 响应式栅格，表格窄屏横向滚动
  3. 纯 CSS 图形：评分条、趋势柱均用 CSS 实现，不引 JS 图表库
  4. 深浅色自适应：跟随系统 prefers-color-scheme
  5. 合规：报告固定附带免责声明

用法:
    python3 render.py metrics.json -o report.html
    python3 render.py metrics.json --scores scores.json -o report.html
"""

import argparse
import html
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

YI = 1e8

# --------------------------------------------------------------------------
# 决策与配色
# --------------------------------------------------------------------------
DECISION_STYLE = {
    "强烈买入": ("#00a86b", "#0d8f5f", "强烈买入"),
    "买入": ("#2e9e5b", "#248a4d", "买入"),
    "买入谨慎": ("#c99700", "#a87e00", "买入·谨慎"),
    "谨慎": ("#e08a00", "#c07800", "谨慎"),
    "中性": ("#8a8f98", "#6f747c", "中性"),
    "回避": ("#d9534f", "#c0392b", "回避"),
    "拒绝": ("#c9302c", "#a02622", "拒绝"),
}

LEVEL_STYLE = {
    "high": ("#d93025", "rgba(217,48,37,.10)", "高风险"),
    "mid": ("#e37400", "rgba(227,116,0,.10)", "中风险"),
    "review": ("#5b6ee1", "rgba(91,110,225,.10)", "待核查"),
    "positive": ("#0d8f5f", "rgba(13,143,95,.10)", "正面"),
}

DIMENSIONS = [
    ("business", "业务质量", 0.30),
    ("management", "管理层", 0.25),
    ("financial", "财务质量", 0.25),
    ("valuation", "估值", 0.20),
]


def esc(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def fmt(v, unit="", digits=2, na="—"):
    if v is None or v == "":
        return na
    if isinstance(v, (int, float)):
        if abs(v) >= 10000:
            return f"{v:,.0f}{unit}"
        return f"{v:,.{digits}f}{unit}"
    return f"{v}{unit}"


def score_color(s):
    s = s or 0
    if s >= 4.0:
        return "#0d8f5f"
    if s >= 3.5:
        return "#2e9e5b"
    if s >= 3.0:
        return "#c99700"
    if s >= 2.5:
        return "#e08a00"
    return "#c9302c"


def decision_of(total: float) -> str:
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
    if total >= 2.0:
        return "回避"
    return "拒绝"


# --------------------------------------------------------------------------
# 组件
# --------------------------------------------------------------------------
def render_header(d: Dict, scores: Dict) -> str:
    meta = d.get("meta", {})
    q = d.get("quote", {})
    total = scores.get("total")
    no_score = total is None
    decision = scores.get("decision") or (decision_of(total) if total is not None else "数据不足")
    c1, c2, label = DECISION_STYLE.get(decision, ("#8a8f98", "#6f747c", decision))
    sc = score_color(total or 0)
    cur = "HK$" if meta.get("currency") == "HKD" else "¥"
    price = q.get("price") or 0
    chg = q.get("change_pct") or 0
    chg_cls = "up" if chg >= 0 else "down"
    confidence = scores.get("confidence") or "—"
    warn = meta.get("warnings") or []

    risk_flag = ""
    if scores.get("risk_flag"):
        risk_flag = f'<div class="risk-flag">⚠️ {esc(scores["risk_flag"])}</div>'

    warns = ""
    if warn:
        # 带【】的是适用性/完整性重大提示，默认展开，避免被折叠漏看
        major = [w for w in warn if w.startswith("【")]
        minor = [w for w in warn if not w.startswith("【")]
        parts = []
        if major:
            lis = "".join(f"<li>{esc(w)}</li>" for w in major)
            parts.append(f'<div class="warn-major"><ul>{lis}</ul></div>')
        if minor:
            lis = "".join(f"<li>{esc(w)}</li>" for w in minor)
            parts.append(
                f'<details class="warnbox"><summary>数据完整度提示（{len(minor)}）</summary>'
                f'<ul>{lis}</ul></details>')
        warns = "".join(parts)

    return f"""
<header class="hero" style="--c1:{c1};--c2:{c2}">
  <div class="hero-top">
    <div class="hero-id">
      <h1>{esc(meta.get('name') or meta.get('query'))}</h1>
      <div class="hero-sub">{esc(meta.get('code'))} · {esc(meta.get('market_name'))} · {esc(meta.get('secucode'))}</div>
    </div>
    <div class="hero-score" style="--sc:{sc}">
      <div class="score-num">{('%.2f' % total) if not no_score else '—'}</div>
      <div class="score-den">{"/ 5.0" if not no_score else "数据不足"}</div>
    </div>
  </div>
  <div class="hero-badges">
    <span class="badge decision">{esc(label)}</span>
    <span class="badge ghost">置信度 {esc(confidence)}</span>
    <span class="badge ghost">{cur}{fmt(price, digits=2)} <b class="{chg_cls}">{chg:+.2f}%</b></span>
    <span class="badge ghost">PE {fmt(q.get('pe_ttm'))} · PB {fmt(q.get('pb'))}</span>
    <span class="badge ghost">市值 {fmt(q.get('mcap_yi'))} 亿</span>
  </div>
  {risk_flag}
  {warns}
</header>"""


def render_dimensions(scores: Dict) -> str:
    dims = scores.get("dimensions") or {}
    rows = []
    for key, label, weight in DIMENSIONS:
        item = dims.get(key) or {}
        s = item.get("score")
        if s is None:
            continue
        pctv = max(0, min(100, (s / 5.0) * 100))
        sc = score_color(s)
        note = item.get("note") or ""
        src = item.get("source") or ""
        src_tag = ""
        if src == "auto":
            src_tag = '<span class="tag auto">自动估算</span>'
        elif src == "llm":
            src_tag = '<span class="tag llm">模型判断</span>'
        rows.append(f"""
    <div class="dim-row">
      <div class="dim-head">
        <span class="dim-name">{esc(label)}</span>
        <span class="dim-weight">权重 {int(weight*100)}%</span>
        <span class="dim-score" style="color:{sc}">{s:.2f}</span>
      </div>
      <div class="bar"><i style="width:{pctv:.1f}%;background:{sc}"></i></div>
      {f'<div class="dim-note">{esc(note)}</div>' if note else ''}
      <div class="dim-src">{src_tag}</div>
    </div>""")
    if not rows:
        return ""
    return f"""<section class="card">
  <h2>四维评分</h2>
  <div class="dims">{"".join(rows)}</div>
  {f'<div class="calc">{esc(scores.get("formula") or "")}</div>' if scores.get("formula") else ''}
</section>"""


def render_metrics(d: Dict) -> str:
    m = d.get("metrics") or {}
    if not m:
        return ""
    groups = [
        ("盈利能力", [
            ("营业收入", m.get("营收亿"), "亿"), ("归母净利润", m.get("归母净利亿"), "亿"),
            ("扣非净利润", m.get("扣非净利亿"), "亿"), ("毛利率", m.get("毛利率%"), "%"),
            ("净利率", m.get("净利率%"), "%"), ("ROE", m.get("ROE%"), "%"),
            ("ROIC", m.get("ROIC%"), "%"),
        ]),
        ("现金流与含金量", [
            ("经营现金流 OCF", m.get("OCF亿"), "亿"), ("OCF/净利润", m.get("OCF/净利"), "×"),
            ("自由现金流", m.get("自由现金流亿"), "亿"), ("投资收益/营业利润", m.get("投资收益/营业利润%"), "%"),
            ("主业经营性利润", m.get("主业经营性利润亿"), "亿"),
        ]),
        ("资本结构", [
            ("货币资金", m.get("货币资金亿"), "亿"), ("交易性金融资产", m.get("交易性金融资产亿"), "亿"),
            ("有息负债", m.get("有息负债亿"), "亿"), ("净现金", m.get("净现金亿"), "亿"),
            ("资产负债率", m.get("资产负债率%"), "%"), ("商誉", m.get("商誉亿"), "亿"),
            ("商誉/净资产", m.get("商誉/净资产%"), "%"), ("合同负债", m.get("合同负债亿"), "亿"),
        ]),
        ("股东回报", [
            ("股息率", m.get("股息率%"), "%"), ("派息率", m.get("派息率%"), "%"),
        ]),
    ]
    blocks = []
    for title, items in groups:
        cells = "".join(
            f'<div class="mc"><div class="mk">{esc(k)}</div>'
            f'<div class="mv">{fmt(v, u)}</div></div>'
            for k, v, u in items if v is not None)
        if cells:
            blocks.append(f'<div class="mgroup"><div class="mgtitle">{esc(title)}</div>'
                          f'<div class="mgrid">{cells}</div></div>')
    period = m.get("最新报告期") or ""
    return f"""<section class="card">
  <h2>核心指标 <span class="period">截至 {esc(period)}</span></h2>
  {"".join(blocks)}
</section>"""


def render_trend(d: Dict) -> str:
    m = d.get("metrics") or {}
    hist = m.get("营收趋势") or []
    if len(hist) < 2:
        return ""
    maxv = max([h.get("营收亿") or 0 for h in hist] + [1])
    bars = []
    for h in hist:
        rev = h.get("营收亿") or 0
        npp = h.get("归母净利亿") or 0
        h1 = max(2, (rev / maxv) * 92)
        h2 = max(1, (abs(npp) / maxv) * 92)
        neg = " neg" if npp < 0 else ""
        ocf = h.get("OCF/净利")
        ocf_txt = f"{ocf:.2f}" if isinstance(ocf, (int, float)) else "—"
        ocf_cls = "warn" if isinstance(ocf, (int, float)) and ocf < 1 else ""
        bars.append(f"""
      <div class="tcol">
        <div class="tstack">
          <i class="tb rev" style="height:{h1:.1f}%" title="营收"></i>
          <i class="tb np{neg}" style="height:{h2:.1f}%" title="归母净利"></i>
        </div>
        <div class="tyear">{esc(h.get('报告期'))}</div>
        <div class="tval">{fmt(rev)}</div>
        <div class="tocf {ocf_cls}">OCF {ocf_txt}</div>
      </div>""")
    return f"""<section class="card">
  <h2>年度经营趋势 <span class="period">营收 / 归母净利（亿元）</span></h2>
  <div class="trend">{"".join(bars)}</div>
  <div class="legend"><span class="lg rev"></span>营业收入
    <span class="lg np"></span>归母净利润</div>
</section>"""


def render_signals(d: Dict) -> str:
    sigs = d.get("signals") or []
    if not sigs:
        return ""
    order = {"high": 0, "mid": 1, "review": 2, "positive": 3}
    sigs = sorted(sigs, key=lambda s: order.get(s.get("level"), 9))
    items = []
    for s in sigs:
        color, bg, label = LEVEL_STYLE.get(s.get("level"), ("#8a8f98", "rgba(0,0,0,.04)", "提示"))
        val = s.get("value")
        if isinstance(val, list):
            detail_extra = "".join(f"<li>{esc(x)}</li>" for x in val[:5])
            detail_extra = f"<ul class='sub'>{detail_extra}</ul>"
        else:
            detail_extra = ""
        items.append(f"""
      <div class="sig" style="--sc:{color};--bg:{bg}">
        <div class="sig-head">
          <span class="sig-tag">{esc(label)}</span>
          <span class="sig-name">{esc(s.get('signal'))}</span>
        </div>
        <div class="sig-detail">{esc(s.get('detail'))}</div>
        {detail_extra}
        {f'<div class="sig-basis">规则：{esc(s.get("basis"))}</div>' if s.get('basis') else ''}
      </div>""")
    return f"""<section class="card">
  <h2>排雷信号 <span class="period">共 {len(sigs)} 条，按风险排序</span></h2>
  <div class="sigs">{"".join(items)}</div>
</section>"""


def render_management(scores: Dict) -> str:
    mg = scores.get("management_detail")
    if not mg:
        return ""
    rows = []
    for k, label in [("integrity", "诚信度"), ("competence", "经营能力"),
                     ("capital_allocation", "资本配置"), ("ownership", "内部人持股"),
                     ("compensation", "薪酬对齐")]:
        v = mg.get(k)
        if v is None:
            continue
        sc = score_color(v)
        pctv = max(0, min(100, (v / 5.0) * 100))
        rows.append(f"""
      <div class="mg-row">
        <span class="mg-name">{esc(label)}</span>
        <div class="bar sm"><i style="width:{pctv:.1f}%;background:{sc}"></i></div>
        <span class="mg-score" style="color:{sc}">{v:.1f}</span>
      </div>""")
    if not rows:
        return ""
    checks = mg.get("checks") or []
    chk = ""
    if checks:
        # 区分「已自动核查」与「待人工核查」：脚本无法自动核查的项必须明确告知
        manual_keywords = ("业绩承诺", "监管", "处罚", "减持", "增持", "董事", "股东权益变动")
        lis_parts = []
        for c in checks:
            is_manual = any(k in (c.get("item") or "") for k in manual_keywords)
            if c.get("flag"):
                tag, cls = "⚠ 命中", "no"
            elif is_manual:
                tag, cls = "🔍 待人工核查", "pending"
            else:
                tag, cls = "✓ 未发现", "ok"
            lis_parts.append(
                f'<li><b>{esc(c.get("item"))}</b>：{esc(c.get("result"))} '
                f'<span class="chk-{cls}">{esc(tag)}</span></li>')
        lis = "".join(lis_parts)
        chk = f'<div class="checks"><div class="cktitle">管理层瑕疵核查（模型强制项）</div><ul>{lis}</ul></div>'
    note = mg.get("note") or ""
    return f"""<section class="card">
  <h2>管理层五维评估 <span class="period">权重 25%</span></h2>
  <div class="mg">{"".join(rows)}</div>
  {f'<div class="dim-note">{esc(note)}</div>' if note else ''}
  {chk}
</section>"""


def render_conclusion(scores: Dict) -> str:
    parts = []
    if scores.get("summary"):
        parts.append(f'<div class="sum">{esc(scores["summary"])}</div>')
    if scores.get("thesis"):
        lis = "".join(f"<li>{esc(x)}</li>" for x in scores["thesis"])
        parts.append(f'<div class="blk"><div class="blkt">核心逻辑</div><ul>{lis}</ul></div>')
    if scores.get("risks"):
        lis = "".join(f"<li>{esc(x)}</li>" for x in scores["risks"])
        parts.append(f'<div class="blk"><div class="blkt">主要风险</div><ul>{lis}</ul></div>')
    if scores.get("watchlist"):
        lis = "".join(f"<li>{esc(x)}</li>" for x in scores["watchlist"])
        parts.append(f'<div class="blk"><div class="blkt">跟踪清单</div><ul>{lis}</ul></div>')
    if not parts:
        return ""
    return f'<section class="card"><h2>结论</h2>{"".join(parts)}</section>'


def render_announcements(d: Dict) -> str:
    anns = d.get("announcements") or []
    if not anns:
        return ""
    lis = "".join(
        f'<li><span class="and">{esc(a.get("date"))}</span>'
        f'<span class="ant">{esc(a.get("title"))}</span></li>'
        for a in anns[:20])
    return f"""<section class="card">
  <h2>近期公告 <span class="period">最近 {min(20, len(anns))} 条</span></h2>
  <ul class="anns">{lis}</ul>
</section>"""


# --------------------------------------------------------------------------
# 样式与组装
# --------------------------------------------------------------------------
CSS = """
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;padding:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",
  "Hiragino Sans GB","Microsoft YaHei",sans-serif;
  font-size:15px;line-height:1.65;letter-spacing:.1px}
:root{
  --bg:#f2f3f5;--fg:#1a1d21;--card:#fff;--muted:#6b7280;
  --line:rgba(0,0,0,.08);--bar:rgba(0,0,0,.06);--sub:rgba(0,0,0,.035)}
@media (prefers-color-scheme:dark){
  :root{--bg:#101114;--fg:#e6e8eb;--card:#1a1c20;--muted:#9aa1ab;
    --line:rgba(255,255,255,.10);--bar:rgba(255,255,255,.08);--sub:rgba(255,255,255,.04)}
}
.wrap{max-width:760px;margin:0 auto;padding:14px 12px 40px}

/* hero */
.hero{border-radius:16px;padding:18px 16px 14px;color:#fff;
  background:linear-gradient(135deg,var(--c1),var(--c2));
  box-shadow:0 6px 20px rgba(0,0,0,.12);margin-bottom:14px}
.hero-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.hero-id h1{margin:0 0 4px;font-size:22px;font-weight:700;letter-spacing:.3px}
.hero-sub{font-size:12.5px;opacity:.88}
.hero-score{text-align:right;flex-shrink:0}
.score-num{font-size:40px;font-weight:800;line-height:1;color:var(--sc,#fff)}
.score-den{font-size:11px;opacity:.85;margin-top:2px}
.hero-badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
.badge{font-size:11.5px;padding:4px 9px;border-radius:20px;
  background:rgba(255,255,255,.20);color:#fff;white-space:nowrap}
.badge.decision{background:#fff;color:var(--c2);font-weight:700}
.badge b.up{color:#b9ffdd}.badge b.down{color:#ffc9c9}
.risk-flag{margin-top:12px;background:rgba(255,255,255,.92);color:#8a2b06;
  padding:8px 11px;border-radius:9px;font-size:13px;font-weight:600}
.warnbox{margin-top:10px;background:rgba(0,0,0,.16);border-radius:9px;
  padding:7px 11px;font-size:12px}
.warnbox summary{cursor:pointer;opacity:.92}
.warnbox ul{margin:6px 0 2px;padding-left:18px;opacity:.9}
.warn-major{margin-top:11px;background:rgba(255,255,255,.94);color:#8a2b06;
  border-radius:9px;padding:9px 11px;font-size:12px;line-height:1.6}
.warn-major ul{margin:0;padding-left:17px}
.warn-major li{margin:2px 0}

/* card */
.card{background:var(--card);border-radius:14px;padding:15px 14px;margin-bottom:12px;
  box-shadow:0 1px 3px rgba(0,0,0,.05)}
.card h2{margin:0 0 12px;font-size:15.5px;font-weight:700;
  display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.period{font-size:11.5px;color:var(--muted);font-weight:400}

/* dimensions */
.dims{display:flex;flex-direction:column;gap:13px}
.dim-head{display:flex;align-items:baseline;gap:8px;margin-bottom:5px}
.dim-name{font-weight:600;font-size:14px}
.dim-weight{font-size:11px;color:var(--muted)}
.dim-score{margin-left:auto;font-weight:800;font-size:16px}
.bar{height:8px;border-radius:5px;background:var(--bar);overflow:hidden}
.bar.sm{height:6px;flex:1}
.bar i{display:block;height:100%;border-radius:5px}
.dim-note{font-size:12.5px;color:var(--muted);margin-top:5px;line-height:1.55}
.dim-src{margin-top:3px}
.tag{font-size:10px;padding:1px 6px;border-radius:4px}
.tag.auto{background:var(--sub);color:var(--muted)}
.tag.llm{background:rgba(13,143,95,.12);color:#0d8f5f}
.calc{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);
  font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}

/* metrics */
.mgroup{margin-bottom:12px}
.mgroup:last-child{margin-bottom:0}
.mgtitle{font-size:12px;color:var(--muted);margin-bottom:6px;font-weight:600}
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:8px}
.mc{background:var(--sub);border-radius:9px;padding:8px 9px}
.mk{font-size:11px;color:var(--muted);margin-bottom:3px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mv{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums}

/* trend */
.trend{display:flex;align-items:flex-end;gap:5px;overflow-x:auto;padding:4px 0 2px}
.tcol{flex:1;min-width:52px;text-align:center}
.tstack{display:flex;align-items:flex-end;justify-content:center;gap:2px;height:100px}
.tb{width:15px;border-radius:3px 3px 0 0;display:block}
.tb.rev{background:#5b8def}
.tb.np{background:#0d8f5f}
.tb.np.neg{background:#c9302c}
.tyear{font-size:11px;color:var(--muted);margin-top:5px}
.tval{font-size:12px;font-weight:700;margin-top:1px;font-variant-numeric:tabular-nums}
.tocf{font-size:10px;color:var(--muted);margin-top:1px}
.tocf.warn{color:#c9302c;font-weight:700}
.legend{font-size:11px;color:var(--muted);margin-top:8px;display:flex;
  align-items:center;gap:5px;flex-wrap:wrap}
.lg{width:10px;height:10px;border-radius:2px;display:inline-block}
.lg.rev{background:#5b8def}.lg.np{background:#0d8f5f}

/* signals */
.sigs{display:flex;flex-direction:column;gap:8px}
.sig{border-left:3px solid var(--sc);background:var(--bg-sig,var(--sub));
  border-radius:0 9px 9px 0;padding:9px 11px}
.sig-head{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.sig-tag{font-size:10px;font-weight:700;color:#fff;background:var(--sc);
  padding:1px 6px;border-radius:4px}
.sig-name{font-weight:600;font-size:13.5px}
.sig-detail{font-size:12.5px;color:var(--muted);margin-top:4px;line-height:1.55}
.sig ul.sub{margin:5px 0 0;padding-left:17px;font-size:11.5px;color:var(--muted)}
.sig-basis{font-size:10.5px;color:var(--muted);opacity:.8;margin-top:4px}

/* management */
.mg{display:flex;flex-direction:column;gap:9px}
.mg-row{display:flex;align-items:center;gap:9px}
.mg-name{font-size:13px;width:74px;flex-shrink:0}
.mg-score{font-size:14px;font-weight:800;width:30px;text-align:right}
.checks{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}
.cktitle{font-size:12px;color:var(--muted);margin-bottom:6px;font-weight:600}
.checks ul{margin:0;padding-left:16px;font-size:12.5px;line-height:1.75}
.chk-ok{color:#0d8f5f;font-size:11px;margin-left:4px}
.chk-no{color:#c9302c;font-size:11px;margin-left:4px;font-weight:700}
.chk-pending{color:#5b6ee1;font-size:11px;margin-left:4px}

/* conclusion */
.sum{font-size:13.5px;line-height:1.75;margin-bottom:12px}
.blk{margin-top:10px}
.blkt{font-size:12px;color:var(--muted);margin-bottom:5px;font-weight:600}
.blk ul{margin:0;padding-left:17px;font-size:13px;line-height:1.75}

/* announcements */
.anns{margin:0;padding:0;list-style:none}
.anns li{display:flex;gap:8px;padding:6px 0;border-bottom:1px solid var(--line);
  font-size:12.5px;align-items:flex-start}
.anns li:last-child{border-bottom:none}
.and{color:var(--muted);flex-shrink:0;font-variant-numeric:tabular-nums;font-size:11.5px}
.ant{flex:1}

/* footer */
.foot{font-size:11px;color:var(--muted);line-height:1.7;padding:4px 6px;
  text-align:center;margin-top:6px}
.foot b{color:var(--fg)}
@media(max-width:400px){
  .mgrid{grid-template-columns:repeat(2,1fr)}
  .score-num{font-size:34px}
  .hero-id h1{font-size:19px}
}
"""

FOOT_TPL = """
<div class="foot">
  <b>免责声明</b>：本报告由「检查表评估模型 v2.2」自动生成，基于公开财报与公告数据的
  <b>客观指标计算与规则化筛查</b>，仅供研究参考。<br>
  不构成任何证券买卖要约或投资建议。市场有风险，决策需自主判断。<br>
  数据采集 {ft} · 引擎 v2.2
</div>
"""


def render_report(d: Dict, scores: Dict) -> str:
    meta = d.get("meta", {})
    ft = meta.get("fetch_time") or datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"{meta.get('name') or meta.get('query')}（{meta.get('code')}）检查表分析报告"
    total = scores.get("total")
    decision = scores.get("decision") or decision_of(total or 0)
    desc = f"综合评分 {total:.2f}/5.0 · {decision}" if total else "检查表分析报告"

    body = "".join([
        render_header(d, scores),
        render_dimensions(scores),
        render_metrics(d),
        render_trend(d),
        render_signals(d),
        render_management(scores),
        render_conclusion(scores),
        render_announcements(d),
        FOOT_TPL.format(ft=esc(ft)),
    ])
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="format-detection" content="telephone=no">
<meta name="description" content="{esc(desc)}">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="检查表分析 - HTML 报告渲染")
    ap.add_argument("metrics", help="collect.py 输出的 JSON")
    ap.add_argument("--scores", help="评分 JSON（由模型判断或 auto_score.py 生成）")
    ap.add_argument("-o", "--out", required=True, help="输出 HTML 路径")
    args = ap.parse_args()

    with open(args.metrics, encoding="utf-8") as f:
        data = json.load(f)
    scores = {}
    if args.scores:
        with open(args.scores, encoding="utf-8") as f:
            scores = json.load(f)

    if not data.get("ok"):
        print(f"数据异常: {data.get('error')}", file=sys.stderr)

    html_out = render_report(data, scores)
    out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"[已生成] {out}  ({len(html_out.encode('utf-8')):,} 字节)")


if __name__ == "__main__":
    main()
