# -*- coding: utf-8 -*-
"""
检查表分析 - 一键入口

    python3 analyze.py 贵州茅台 -o ./reports

流程：解析标的 → 采集数据与指标 → 规则化自动打分 → 渲染 HTML

可选：
    --scores x.json   用外部（模型判断）评分覆盖自动分
    --keep-json       保留中间 JSON，供人工复核或二次渲染
    --periods N       财报期数（默认 12）
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402
import auto_score  # noqa: E402
import render  # noqa: E402


def run(query: str, periods: int = 12, with_anns: bool = True,
        scores_path: str = None, outdir: str = None, keep_json: bool = False):
    t0 = datetime.now()

    # 1. 采集
    data = collect.collect(query, periods=periods, with_anns=with_anns)
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error")}

    meta = data["meta"]
    name = meta.get("name") or query
    code = meta.get("code")

    # 2. 打分
    if scores_path:
        with open(scores_path, encoding="utf-8") as f:
            scores = json.load(f)
        src = "模型判断"
    else:
        scores = auto_score.auto_score(data)
        src = "规则引擎自动打分"

    # 3. 渲染
    outdir = outdir or os.getcwd()
    os.makedirs(outdir, exist_ok=True)
    safe = "".join(c for c in f"{name}_{code}" if c not in r'\/:*?"<>|')
    html_path = os.path.join(outdir, f"{safe}_检查表分析报告.html")
    html_out = render.render_report(data, scores)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_out)

    if keep_json:
        with open(os.path.join(outdir, f"{safe}_metrics.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        with open(os.path.join(outdir, f"{safe}_scores.json"), "w", encoding="utf-8") as f:
            json.dump(scores, f, ensure_ascii=False, indent=2)

    elapsed = (datetime.now() - t0).total_seconds()
    m = data.get("metrics") or {}
    return {
        "ok": True, "html": html_path, "name": name, "code": code,
        "secucode": meta.get("secucode"), "source": src,
        "total": scores.get("total"), "decision": scores.get("decision"),
        "confidence": scores.get("confidence"),
        "risk_flag": scores.get("risk_flag"),
        "dimensions": {k: v.get("score") for k, v in (scores.get("dimensions") or {}).items()},
        "signals": len(data.get("signals") or []),
        "high_signals": len([s for s in (data.get("signals") or [])
                             if s.get("level") == "high"]),
        "price": (data.get("quote") or {}).get("price"),
        "pe": (data.get("quote") or {}).get("pe_ttm"),
        "roe": m.get("ROE%"),
        "elapsed_sec": round(elapsed, 1),
        "bytes": len(html_out.encode("utf-8")),
    }


def main():
    ap = argparse.ArgumentParser(description="检查表分析 - 一键生成 HTML 报告")
    ap.add_argument("query", help="股票名称 / 代码 / 拼音")
    ap.add_argument("-o", "--outdir", default=".", help="输出目录")
    ap.add_argument("--scores", help="外部评分 JSON（覆盖自动打分）")
    ap.add_argument("--periods", type=int, default=12)
    ap.add_argument("--no-anns", action="store_true", help="跳过公告（更快）")
    ap.add_argument("--keep-json", action="store_true", help="保留中间 JSON")
    ap.add_argument("--quiet", action="store_true", help="仅输出 HTML 路径")
    args = ap.parse_args()

    r = run(args.query, periods=args.periods, with_anns=not args.no_anns,
            scores_path=args.scores, outdir=args.outdir, keep_json=args.keep_json)

    if not r.get("ok"):
        print(f"[失败] {r.get('error')}", file=sys.stderr)
        sys.exit(1)

    if args.quiet:
        print(r["html"])
        return

    dims = r.get("dimensions") or {}
    print(f"""
[检查表分析完成] {r['name']}（{r['secucode']}）
  综合评分  {r['total']} / 5.0      决策：{r['decision']}   置信度：{r['confidence']}
  四维     业务 {dims.get('business')} · 管理层 {dims.get('management')} · 财务 {dims.get('financial')} · 估值 {dims.get('valuation')}
  排雷信号 {r['signals']} 条（其中高风险 {r['high_signals']} 条）
  打分方式 {r['source']}
  用时     {r['elapsed_sec']}s
  输出     {r['html']}（{r['bytes']:,} 字节）""")
    if r.get("risk_flag"):
        print(f"  ⚠️  {r['risk_flag']}")


if __name__ == "__main__":
    main()
