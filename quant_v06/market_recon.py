#!/usr/bin/env python3
"""大范围侦查 v2: 全行业动量排行 + 持仓健康检查
数据源: 新浪免费行情API(无需key)
"""
import json, urllib.request, re

# 行业池: 8大行业 → 新浪指数代码
SECTOR_INDEX = {
    "白酒": "sz399997",
    "银行": "sz399986",
    "医药": "sh000013",
    "科技": "sz399006",   # 创业板指(科技含量高)
    "新能源": "sz399976",  # 新能源车
    "证券": "sz399975",
    "传媒": "sz399971",
    "军工": "sh399967",
}
MARKET_INDEX = "sz399300"  # 沪深300

# 个股→行业映射(龙头股池)
STOCK_SECTOR = {
    "600519": "白酒", "000858": "白酒",
    "600036": "银行", "601166": "银行",
    "600276": "医药", "603259": "医药",
    "000063": "科技", "002415": "科技",
    "300750": "新能源", "002594": "新能源",
    "600030": "证券", "300059": "证券",
    "002027": "传媒", "300413": "传媒",
}

def _code_to_sina(symbol):
    """600xxx→sh600xxx, 000/300/002→sz"""
    if symbol.startswith("6"):
        return "sh" + symbol
    return "sz" + symbol

def fetch_quotes(codes):
    url = f"https://hq.sinajs.cn/list={','.join(codes)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode("gbk", "replace")
    out = {}
    for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', text):
        code, data = m.groups()
        f = data.split(",")
        if len(f) > 32:
            prev_close = float(f[2])
            last = float(f[3])
            if last <= 0:
                last = prev_close
            chg = 0.0 if prev_close <= 0 else round((last - prev_close) / prev_close * 100, 2)
            out[code] = {
                "name": f[0], "last": last, "prev_close": prev_close,
                "chg_pct": chg, "date": f[30], "time": f[31],
            }
    return out

def sector_rankings():
    """全行业动量排行. 返回(list按涨幅降序, dict行情)"""
    codes = [MARKET_INDEX] + list(SECTOR_INDEX.values())
    q = fetch_quotes(codes)
    mkt = q.get(MARKET_INDEX, {})
    rows = []
    for sector, idx_code in SECTOR_INDEX.items():
        d = q.get(idx_code)
        if d:
            rows.append({"sector": sector, "chg_pct": d["chg_pct"], "last": d["last"]})
    rows.sort(key=lambda x: -x["chg_pct"])
    return mkt.get("chg_pct", 0.0), rows, q

def holdings_health(positions, q):
    """持仓健康检查: 每个持仓行业的表现 vs 大盘. positions={'600519': {...}}"""
    sectors = {}
    for sym in positions or {}:
        ind = STOCK_SECTOR.get(sym, "其他")
        idx_code = SECTOR_INDEX.get(ind)
        if not idx_code:
            continue
        d = q.get(idx_code)
        if d:
            sectors.setdefault(ind, d["chg_pct"])
    weak = [(s, c) for s, c in sectors.items() if c < 0]
    return sectors, weak

def build_recon_report(symbol, positions=None):
    """为个股分析生成全行业侦查报告(注入custom_prompt)"""
    mkt_chg, rows, q = sector_rankings()
    ind = STOCK_SECTOR.get(symbol, "未知")
    stock_code = _code_to_sina(symbol)
    stock = q.get(stock_code, {})
    my_row = next((r for r in rows if r["sector"] == ind), None)
    top3 = rows[:3]
    weak = [r["sector"] for r in rows if r["chg_pct"] < 0]

    lines = [
        f"[全行业侦查 · 动量排行] ({rows[0]['last'] if rows else ''} {q.get(stock_code,{}).get('time','')})",
        f"大盘(沪深300): {mkt_chg:+.2f}%",
        "行业排行(强→弱):",
    ]
    for i, r in enumerate(rows, 1):
        mark = " ←持仓行业" if r["sector"] == ind else ""
        lines.append(f"  {i}. {r['sector']}: {r['chg_pct']:+.2f}%{mark}")
    if stock:
        lines.append(f"标的 {stock.get('name', symbol)}: {stock['chg_pct']:+.2f}% (最新{stock['last']})")

    rules = []
    # 持仓行业健康
    holdings_secs, weak_holdings = holdings_health(positions, q)
    if my_row and my_row["sector"] in weak:
        rules.append(f"⚠️ 持仓行业[{ind}]今日跑输大盘, 处于弱势行业列表({', '.join(weak)}): 若该行业连续3日走弱, 必须评估减仓换仓")
    # 强势行业约束
    if top3 and ind not in [r["sector"] for r in top3]:
        tops = ", ".join(f"{r['sector']}({r['chg_pct']:+.2f}%)" for r in top3)
        rules.append(f"⚠️ 标的所属行业[{ind}]不在当日强势TOP3({tops}): 买入信号需额外强理由")
    # 分散要求
    if positions:
        hold_sectors = set(STOCK_SECTOR.get(s, "其他") for s in positions)
        if len(hold_sectors) < 3:
            rules.append(f"⚠️ 当前组合仅覆盖{len(hold_sectors)}个行业({', '.join(hold_sectors)}): 下一笔买入必须是新行业龙头, 提高分散度")
    lines.append("硬约束(必须遵守):")
    lines += [f"  {r}" for r in rules] if rules else ["  (无特殊约束)"]
    return "\n".join(lines), q, rows

def recon_for(symbol, positions=None):
    """兼容v0.4接口: 返回(报告文本, 行情dict)"""
    try:
        report, q, rows = build_recon_report(symbol, positions)
        return report, q
    except Exception as e:
        return f"(侦查失败: {str(e)[:80]}, 按个股独立分析, 保守评估)", {}

if __name__ == "__main__":
    pos = {"600519": {"shares": 100}, "000858": {"shares": 1300}}
    for sym in ("600519", "600036"):
        txt, _ = recon_for(sym, pos)
        print(f"===== {sym} =====")
        print(txt)
        print()
