#!/usr/bin/env python3
"""双审查修复验证测试(clamp/TTL/重连/防饿死/checkpoint滚动)"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from state.server import StateClient
from state.model import STRENGTH_MAX, DECAY_MIN, P0_TTL

PASS, FAIL = "✅", "❌"
results = []
def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{PASS if cond else FAIL} {name}" + (f" (f{detail})" if detail else ""))

c = StateClient()
# 清场: 测试隔离(避免test_state残留信号干扰LIMIT断言)
for s_ in c.signals():
    c.drop(s_["sig_id"])

# F1 强度clamp(信号劫持修复: emit 999 → 服务端压到10)
r = c.emit("clamp_test", {}, strength=999, decay=0.01, source="test")
sid = r["sig_id"]
s = next(s for s in c.signals() if s["sig_id"] == sid)
check("F1 强度clamp≤10", s["strength"] <= STRENGTH_MAX, f"{s['strength']}")

# F2 decay clamp(永久驻留修复: decay=0 → 强制≥0.01)
sid = c.emit("decay_zero_test", {}, decay=0, source="test")["sig_id"]
s = next(s for s in c.signals() if s["sig_id"] == sid)
check("F2 decay强制≥0.01", s["decay"] >= DECAY_MIN, f"{s['decay']}")

# F3 P0信号TTL(created记录, 24h过期逻辑存在性——直接验证created被存)
r = c.emit("master_message", {"text": "TTL测试"}, source="test")
sid = r["sig_id"]
s = next(s for s in c.signals() if s["sig_id"] == sid)
check("F3 P0信号记录created(TTL基础)", abs(s["created"] - time.time()) < 5)

# F4 signals查询LIMIT 20(发25条, 只回≤20)
for i in range(25):
    c.emit("limit_test", {"i": i}, decay=0.01, source="test")
time.sleep(1)
sigs = [s for s in c.signals() if s["sig_type"] == "limit_test"]
check("F4 查询LIMIT≤20", len(sigs) <= 20, f"{len(sigs)}条")

# F5 checkpoint滚动保留5份
import glob
cps = sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "checkpoints", "state_*.db")))
check("F5 快照≤5份滚动", len(cps) <= 5, f"{len(cps)}份")

# F6 重连: 杀服务→重启→client自动恢复(模拟服务重启场景)
c.close()
time.sleep(1)
# (服务由测试外部管理, 这里验证client能重新连上——服务还在跑)
c2 = StateClient()
check("F6 重连后可用", c2._req(op="ping")["ok"] is True)

# F7 P0 TTL过期(压缩时间轴: 直接验证expired逻辑)
from state.model import Signal
sig = Signal("master_message", {}, created=time.time() - P0_TTL - 10)
check("F7 P0 TTL过期判定", sig.expired() is True)
sig2 = Signal("master_message", {}, created=time.time())
check("F8 新P0信号未过期", sig2.expired() is False)

# F9 防饿死(P2信号保底: 清场→发task_done→衰减→验证保底)
for s_ in c.signals():
    if s_["sig_type"] in ("limit_test", "clamp_test", "decay_zero_test", "focus_test"):
        c.drop(s_["sig_id"])
c.emit("task_done", {"x": 1}, decay=0.3, source="test")
time.sleep(1.2)
sigs = [s for s in c.signals() if s["sig_type"] == "task_done"]
# 保底强度 = threshold*0.6 = 0.15 (task_done衰减后不低于0.15)
check("F9 P2信号保底防饿死", any(s["strength"] >= 0.14 for s in sigs),
      f"min={[round(s['strength'],3) for s in sigs][:3]}")

c2.close()
c.close()
print(f"\n{'='*40}")
p = sum(1 for _, ok in results if ok)
print(f"修复验证: {p}/{len(results)} 通过")
sys.exit(0 if p == len(results) else 1)
