#!/usr/bin/env python3
"""轻量股票模拟盘（Paper Trading）v0.2
信号输入：TradingAgents-CN 分析报告（action/target_price/confidence）
成交规则：信号次日开盘价成交（含滑点）
风控护栏：单票≤10% · 单行业≤25% · 总仓≤70% · 最大回撤熔断10% · 连亏3次暂停5天
成本模型：佣金万2.5（最低5元，买卖双向）· 印花税千0.5（仅卖出）· 滑点0.1%
"""
import json, os
from datetime import datetime, timedelta

STATE_FILE = "/opt/quant-pipeline/paper/state.json"

# ---------- 风控参数 ----------
SINGLE_STOCK_LIMIT = 0.10   # 单票 ≤10% 总资产
INDUSTRY_LIMIT = 0.25       # 单行业 ≤25% 总资产
TOTAL_LIMIT = 0.70          # 总仓位 ≤70% 总资产

# ---------- 行业映射(v0.6 多行业分散: 7行业×2龙头) ----------
STOCK_INDUSTRY = {
    # 白酒
    "600519": "白酒", "000858": "白酒",
    # 银行
    "600036": "银行", "601166": "银行",
    # 医药
    "600276": "医药", "603259": "医药",
    # 科技
    "000063": "科技", "002415": "科技",
    # 新能源
    "300750": "新能源", "002594": "新能源",
    # 证券
    "600030": "证券", "300059": "证券",
    # 传媒
    "002027": "传媒", "300413": "传媒",
}

# ---------- 成本参数 ----------
COMMISSION_RATE = 0.00025   # 佣金 万2.5
MIN_COMMISSION = 5.0        # 最低佣金 5 元
STAMP_TAX_RATE = 0.0005     # 印花税 千0.5（2023-08 起，仅卖出）
SLIPPAGE_RATE = 0.001       # 滑点 0.1%


class PaperTrading:
    def __init__(self, initial_cash=1000000.0, state_file=STATE_FILE):
        self.state_file = state_file
        if os.path.exists(state_file):
            with open(state_file) as f:
                self.s = json.load(f)
        else:
            self.s = {
                "cash": initial_cash,
                "positions": {},          # {symbol: {shares, avg_cost}}
                "history": [],            # 每日净值记录
                "pending_signals": [],
                "peak_equity": initial_cash,
                "consecutive_losses": 0,
                "pause_until": None,
                "trades": [],
                "processed_reports": [],
                "risk_exceptions": [],    # 风控例外记录（如规则上线前的既有持仓）
                "created_at": datetime.now().isoformat(),
            }

    def save(self):
        with open(self.state_file, "w") as f:
            json.dump(self.s, f, ensure_ascii=False, indent=2)

    def equity(self, ohlc=None):
        """总资产 = 现金 + 持仓市值（按传入的最新收盘价估值）"""
        val = self.s["cash"]
        for sym, pos in self.s["positions"].items():
            px = (ohlc or {}).get(sym, {}).get("close")
            if px:
                val += pos["shares"] * px
            else:
                val += pos["shares"] * pos["avg_cost"]
        return val

    # ---------- 成本计算 ----------
    def _calc_cost(self, amount, side):
        """交易成本：佣金（双向，最低5元）+ 印花税（仅卖出）"""
        commission = max(amount * COMMISSION_RATE, MIN_COMMISSION)
        stamp = amount * STAMP_TAX_RATE if side == "SELL" else 0.0
        return commission + stamp

    # ---------- 风控检查 ----------
    def _industry_value(self, ohlc):
        """按行业统计当前持仓市值"""
        ind_val = {}
        for sym, pos in self.s["positions"].items():
            ind = STOCK_INDUSTRY.get(sym, "其他")
            px = (ohlc or {}).get(sym, {}).get("close", pos["avg_cost"])
            ind_val[ind] = ind_val.get(ind, 0) + pos["shares"] * px
        return ind_val

    def _check_buy_risk(self, sym, budget, ohlc):
        """下单前风控：行业白名单/单票/行业/总仓。返回 (ok, msg)"""
        eq = self.equity(ohlc)
        # 0) 行业白名单：查不到行业分类 → 拒绝下单（不静默放行）
        ind = STOCK_INDUSTRY.get(sym)
        if ind is None:
            return False, f"{sym} 无行业分类数据，禁止交易（白名单机制）"
        # 1) 单票
        pos = self.s["positions"].get(sym)
        cur_val = pos["shares"] * (ohlc or {}).get(sym, {}).get("close", pos["avg_cost"]) if pos else 0
        if (cur_val + budget) / eq > SINGLE_STOCK_LIMIT:
            return False, f"单票仓位超限（{sym} 现{cur_val/eq:.1%}+预算{budget/eq:.1%}>{SINGLE_STOCK_LIMIT:.0%}）"
        # 2) 行业
        ind_val = self._industry_value(ohlc).get(ind, 0)
        if (ind_val + budget) / eq > INDUSTRY_LIMIT:
            return False, f"{ind}行业仓位超限（{ind_val/eq:.1%}+预算{budget/eq:.1%}>{INDUSTRY_LIMIT:.0%}）"
        # 3) 总仓
        total_val = sum(
            p["shares"] * (ohlc or {}).get(s, {}).get("close", p["avg_cost"])
            for s, p in self.s["positions"].items()
        )
        if (total_val + budget) / eq > TOTAL_LIMIT:
            return False, f"总仓位超限（{total_val/eq:.1%}+预算{budget/eq:.1%}>{TOTAL_LIMIT:.0%}）"
        return True, None

    # ---------- 信号 ----------
    def add_signal(self, date, symbol, action, target_price=None, confidence=None, reason=""):
        self.s["pending_signals"].append({
            "date": date, "symbol": symbol, "action": action,
            "target_price": target_price, "confidence": confidence, "reason": reason
        })
        self.save()

    # ---------- 每日 tick ----------
    def tick(self, date, ohlc, price_field="open"):
        self._execute_signals(date, ohlc, price_field=price_field)
        eq = self.equity(ohlc)
        self.s["peak_equity"] = max(self.s["peak_equity"], eq)
        # 口径标注：v0.2 起含成本+风控（8/13 新记录起生效，历史记录不加——复盘分段统计）
        self.s["history"].append({
            "date": date, "equity": round(eq, 2),
            "cost_included": True, "risk_control_active": True,
        })
        self._risk_check(date, ohlc)
        self.save()
        return eq

    def _execute_signals(self, date, ohlc, price_field="open"):
        pending, self.s["pending_signals"] = self.s["pending_signals"], []
        for sig in pending:
            if self.s["pause_until"] and date < self.s["pause_until"]:
                self.s["pending_signals"].append(sig)
                continue
            sym = sig["symbol"]
            px = ohlc.get(sym, {}).get(price_field) or ohlc.get(sym, {}).get("open")
            if not px or px <= 0:
                self.s["pending_signals"].append(sig)
                continue
            pos = self.s["positions"].get(sym)
            if sig["action"] == "BUY":
                eq = self.equity(ohlc)
                budget = min(eq * SINGLE_STOCK_LIMIT, self.s["cash"])
                if budget < 1000:
                    continue
                # 下单前风控（行业白名单/单票/行业/总仓）
                ok, msg = self._check_buy_risk(sym, budget, ohlc)
                if not ok:
                    # 记录被拒信号详情（含预算与行业余量——为 V2 降额买入留数据）
                    ind = STOCK_INDUSTRY.get(sym, "无")
                    eq_now = self.equity(ohlc)
                    ind_val = self._industry_value(ohlc).get(ind, 0)
                    self.s["risk_exceptions"].append({
                        "date": date, "symbol": sym, "action": "BUY_拒绝", "reason": msg,
                        "would_be_size": round(budget, 2),
                        "available_room": round(max(0, eq_now * INDUSTRY_LIMIT - ind_val), 2),
                    })
                    print(f"🚫 {sym} BUY 拒绝: {msg}")
                    continue
                # 滑点加价成交
                buy_px = px * (1 + SLIPPAGE_RATE)
                shares = int(budget / buy_px / 100) * 100     # A股整手
                if shares <= 0:
                    # 高价股修正：预算买不起一手 → 提升到一手成本（可能略超单票限制，一手保底优先）
                    one_lot_cost = int(buy_px * 100)
                    if one_lot_cost > self.s["cash"]:
                        continue
                    shares = 100
                    budget = one_lot_cost
                cost = shares * buy_px
                fee = self._calc_cost(cost, "BUY")
                if cost + fee > self.s["cash"]:
                    continue
                self.s["cash"] -= (cost + fee)
                if pos:
                    total = pos["shares"] + shares
                    pos["avg_cost"] = (pos["avg_cost"] * pos["shares"] + cost) / total
                    pos["shares"] = total
                else:
                    self.s["positions"][sym] = {"shares": shares, "avg_cost": buy_px}
                self.s["trades"].append({"date": date, "symbol": sym, "action": "BUY",
                                         "price": round(buy_px, 2), "shares": shares,
                                         "fee": round(fee, 2), "reason": sig["reason"]})
                print(f"✅ {sym} BUY {shares}股@{buy_px:.2f} 费{fee:.2f}")
            elif sig["action"] == "SELL" and pos:
                # 滑点折价成交 + 成本（佣金+印花税）
                sell_px = px * (1 - SLIPPAGE_RATE)
                proceeds = pos["shares"] * sell_px
                fee = self._calc_cost(proceeds, "SELL")
                pnl = proceeds - fee - pos["avg_cost"] * pos["shares"]
                self.s["cash"] += (proceeds - fee)
                self.s["trades"].append({"date": date, "symbol": sym, "action": "SELL",
                                         "price": round(sell_px, 2), "shares": pos["shares"],
                                         "fee": round(fee, 2),
                                         "pnl": round(pnl, 2), "reason": sig["reason"]})
                del self.s["positions"][sym]
                if pnl < 0:
                    self.s["consecutive_losses"] += 1
                else:
                    self.s["consecutive_losses"] = 0
                print(f"✅ {sym} SELL {pos['shares']}股@{sell_px:.2f} 费{fee:.2f} 盈亏{pnl:.2f}")

    def _risk_check(self, date, ohlc):
        if self.s["pause_until"] and date < self.s["pause_until"]:
            return
        self.s["pause_until"] = None
        # 1) 最大回撤熔断 10%
        eq = self.equity(ohlc)
        if self.s["peak_equity"] > 0:
            dd = (self.s["peak_equity"] - eq) / self.s["peak_equity"]
            if dd > 0.10:
                for sym in list(self.s["positions"]):
                    pos = self.s["positions"][sym]
                    px = ohlc.get(sym, {}).get("close", pos["avg_cost"])
                    self.s["cash"] += pos["shares"] * px
                    del self.s["positions"][sym]
                self.s["pause_until"] = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
                self.s["history"].append({"date": date, "event": "⚠️ 回撤>10% 熔断清仓，暂停7天"})
                return
        # 2) 连续亏损 3 次暂停
        if self.s["consecutive_losses"] >= 3:
            self.s["pause_until"] = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")
            self.s["consecutive_losses"] = 0
            self.s["history"].append({"date": date, "event": "⚠️ 连亏3次，暂停5天"})

    def status(self):
        return {
            "cash": round(self.s["cash"], 2),
            "positions": self.s["positions"],
            "consecutive_losses": self.s["consecutive_losses"],
            "pause_until": self.s["pause_until"],
            "trades": len(self.s["trades"]),
            "history_points": len(self.s["history"]),
            "risk_exceptions": len(self.s["risk_exceptions"]),
        }
