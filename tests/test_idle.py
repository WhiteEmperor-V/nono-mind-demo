#!/usr/bin/env python3
"""闲时反思(iek reflection)验证:
1. 空闲检测: 注入信号处理完毕后, 空闲超过IDLE_REFLECT_SECONDS → emit reflection_trigger
2. 30分钟下限: last_idle_reflection刚写入时, 再空闲也不触发(防风暴)
3. 轻反思入口: reflection.handle(reflection_trigger) → idle_reflect只处理增量对话
需要State服务在跑(python3 state/server.py).
"""
import sys, os, time, json, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from state.server import StateClient

PASS, FAIL = "✅", "❌"
results = []
def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{PASS if cond else FAIL} {name}" + (f" ({detail})" if detail else ""))

c = StateClient()

# --- I-1 单元级: 空闲阈值未到不触发 ---
os.environ["IDLE_REFLECT_SECONDS"] = "3"
os.environ["IDLE_REFLECT_MIN_INTERVAL"] = "60"
import importlib
import core.loop as loop
importlib.reload(loop)          # 让模块级环境变量生效

c.kv_del("last_idle_reflection") # 清场
now = time.time()
_, trig = loop.maybe_idle_reflect(c, now - 1, now=now)
check("I-1a 空闲1s<阈值3s 不触发", trig is False)

last_active, trig = loop.maybe_idle_reflect(c, now - 10, now=now)
check("I-1b 空闲10s>阈值3s 触发", trig is True)
sigs = [s for s in c.signals() if s["sig_type"] == "reflection_trigger"]
check("I-1c 发出reflection_trigger信号(decay=0.05)", bool(sigs) and
      abs(sigs[-1]["decay"] - 0.05) < 1e-9, f"n={len(sigs)}")
kv = c.kv_get("last_idle_reflection")
check("I-1d kv记录last_idle_reflection", bool(kv) and kv.get("at", 0) > 0)
for s in sigs:
    c.drop(s["sig_id"])

# --- I-2 30分钟下限(防风暴) ---
c.kv_set("last_idle_reflection", {"at": time.time()})   # 刚反思过
_, trig = loop.maybe_idle_reflect(c, time.time() - 3600)  # 即使空闲1小时
check("I-2a 下限内再空闲不触发", trig is False)

c.kv_set("last_idle_reflection", {"at": time.time() - 70})  # 超过60s下限
_, trig = loop.maybe_idle_reflect(c, time.time() - 3600)
check("I-2b 超过下限后重新触发", trig is True)
for s in c.signals():
    if s["sig_type"] == "reflection_trigger":
        c.drop(s["sig_id"])
c.kv_del("last_idle_reflection")

# --- I-3 端到端: loop常驻线程 + 注入信号后空闲 ---
os.environ["IDLE_REFLECT_SECONDS"] = "3"
importlib.reload(loop)
import organs.reflection as reflection
c.kv_set("idle_reflect_cursor", {"day": time.strftime("%Y-%m-%d"), "count": 10**9, "at": time.time()})  # 无增量→不耗LLM
t = threading.Thread(target=loop.main, kwargs={"interval": 0.3}, daemon=True)
t.start()
ok3 = False   # 空闲>3s → 应触发(宽限12s, loop可能先消化前序测试残留信号)
for _ in range(24):
    if c.kv_get("last_idle_reflection"):
        ok3 = True
        break
    time.sleep(0.5)
sigs = [s for s in c.signals() if s["sig_type"] == "reflection_trigger"]
kv = c.kv_get("last_idle_reflection")
check("I-3a loop空闲自动发起反思(kv已记)", ok3, f"pending={len(sigs)}")
check("I-3b reflection_trigger已被loop吸收或待处理", True)
for s in sigs:
    c.drop(s["sig_id"])

# --- I-4 轻反思增量: 追加新对话 → handle走idle_reflect ---
day = time.strftime("%Y-%m-%d")
shard = c.kv_get(f"dialogue_history/{day}") or []
c.kv_set("idle_reflect_cursor", {"day": day, "count": len(shard), "at": time.time()})
c.kv_append(f"dialogue_history/{day}", {"role": "user", "content": "闲时反思测试条目"})
c.kv_append(f"dialogue_history/{day}", {"role": "assistant", "content": "收到,这是测试"})
os.environ["REFLECTION_NO_LLM"] = "1"    # 测试不打真实LLM
reflection.handle("reflection_trigger", {"reason": "idle"}, c)
cur = c.kv_get("idle_reflect_cursor")
check("I-4a 轻反思推进游标(增量2条)", cur.get("count") == len(shard) + 2,
      f"count={cur.get('count')}")
journal = os.path.join(reflection.WIKI_DIR, f"{day}.md")
ok = os.path.exists(journal) and "闲时随笔" in open(journal).read()
check("I-4b journal追加闲时随笔", ok, journal)
reflection.handle("task_done", {}, c)   # 非trigger: 吸收不处理
check("I-4c 非trigger信号吸收不动作", (c.kv_get("idle_reflect_cursor") or {}).get("count") == cur["count"])

c.kv_del("last_idle_reflection")
n_fail = sum(1 for _, ok in results if not ok)
print(f"\n{'全部通过' if n_fail == 0 else f'{n_fail}项失败'} ({len(results)}项)")
sys.exit(1 if n_fail else 0)
