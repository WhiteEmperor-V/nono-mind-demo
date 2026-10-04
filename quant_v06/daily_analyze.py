#!/usr/bin/env python3
"""每日自动分析 v0.4（方案A: 大范围侦查/板块联动 + 方案B: 预期持有期约束）
v0.3 → v0.4 变更:
  1. 分析前先跑"大范围侦查"(market_recon), 战场态势+硬约束注入custom_prompt
  2. custom_prompt同时要求: 信号必须带预期持有期标签(日内/波段/中线) + 说明依据因子
cron 不变: 9:30 / 14:45
"""
import json, sys, time, urllib.request
sys.path.insert(0, "/opt/quant-pipeline/paper")
from market_recon import recon_for

API = "http://localhost:8000"
USER, PASS = "admin", "admin123"
STOCKS = ["600519", "000858"]  # 贵州茅台 / 五粮液

HOLDING_PERIOD_RULE = """
【信号结构化要求(方案B)】每个买入/卖出建议必须包含:
- 预期持有期: 日内 / 波段(3-10天) / 中线(10-30天) 三选一
- 依据因子: 用一句话说明该信号主要依据什么(估值/动量/消息/板块联动)
- 止损位: 明确价格
"""

# 打回重析时的强化段 (daily_bridge.py 信号硬校验使用)
REJECT_REINFORCE = """
【上次输出不合格】缺少预期持有期/止损位。
你必须且只能在结论第一行输出以下固定格式:
信号: 买入/卖出 | 持有期: 日内/波段/中线 | 止损: XX.XX元 | 依据: XX
"""


def api_post(path, data, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, data=json.dumps(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"❌ POST {path} 失败: {e}")
        return None


def api_get(path, token):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"❌ GET {path} 失败: {e}")
        return None


def main():
    login = api_post("/api/auth/login", {"username": USER, "password": PASS})
    if not login or not login.get("success"):
        print("❌ 登录失败")
        return
    token = login["data"]["access_token"]
    print("✅ 登录成功")

    for symbol in STOCKS:
        # ── v0.4: 大范围侦查(板块联动, 方案A) ──
        # 读当前持仓(供持仓健康检查)
        try:
            _st = json.load(open("/opt/quant-pipeline/paper/state.json"))
            _pos = list(_st.get("positions", {}).keys())
        except Exception:
            _pos = []
        recon_text, quotes = recon_for(symbol, positions=_pos)
        if recon_text:
            print(f"🔍 {symbol} 战场态势:\n{recon_text}")
        else:
            print(f"⚠️ {symbol} 侦查数据不可用, 退化为v0.3模式")
            recon_text = "(侦查数据不可用, 本次按个股独立分析, 置信度自行保守评估)"

        custom_prompt = (
            f"{recon_text}\n"
            f"{HOLDING_PERIOD_RULE}\n"
            "【板块联动规则(方案A)】你的分析必须先评估上述战场态势再下结论:"
            "板块与个股方向背离时必须解释原因; 违反硬约束的买入建议视为无效。\n"
            "【多行业分散铁律(v0.6)】组合必须覆盖至少3个不同行业; 持仓行业连续3日跑输大盘时,"
            "必须严肃评估减仓换仓; 买入候选仅限当日强势行业TOP3的龙头股; "
            "白酒持仓已近25%上限, 禁止任何加仓白酒的建议。"
        )

        resp = api_post("/api/analysis/single", {
            "symbol": symbol,
            "parameters": {
                "research_depth": "标准",
                "custom_prompt": custom_prompt,
                "selected_analysts": ["market", "fundamentals", "news", "social"],
            },
        }, token)
        if not resp or not resp.get("success"):
            print(f"❌ {symbol} 提交失败: {resp}")
            continue
        task_id = resp["data"]["task_id"]
        print(f"⏳ {symbol} 分析任务 {task_id[:8]} 提交, 等待完成...")
        for i in range(40):
            time.sleep(15)
            st = api_get(f"/api/analysis/tasks/{task_id}/status", token)
            if not st:
                continue
            d = st.get("data", {})
            status = d.get("status")
            if status == "completed":
                print(f"✅ {symbol} 分析完成(进度 {d.get('progress')}%)")
                break
            if status == "failed":
                print(f"❌ {symbol} 分析失败: {str(d.get('error_message'))[:100]}")
                break
            if i % 4 == 0:
                print(f"   ...进度 {d.get('progress')}% ({status})")
        else:
            print(f"⚠️ {symbol} 分析超时未完成")

    print("✅ 今日分析完成(v0.4)")


if __name__ == "__main__":
    main()
