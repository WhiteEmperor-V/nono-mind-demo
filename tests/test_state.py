#!/usr/bin/env python3
"""StateBroker 测试套件(要求: State服务已启动)"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from state.server import StateClient

PASS, FAIL = "✅", "❌"
results = []
def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{PASS if cond else FAIL} {name}" + (f" ({detail})" if detail else ""))

def fresh_client():
    return StateClient()

# T1 服务连通
c = fresh_client()
r = c._req(op="ping")
check("T1 服务连通", r.get("ok") is True, f"revision={r.get('revision')}")

# T2 信号发射+读取
sig_id = c.emit("test_signal", {"v": 1}, decay=0, source="test")["sig_id"]
sigs = c.signals()
found = any(s["sig_id"] == sig_id for s in sigs)
check("T2 信号发射+读取", found)

# T3 信号衰减(P3信号 decay=0.9 两tick后应大降)
sid = c.emit("decay_test", {}, decay=0.9, source="test")["sig_id"]
s0 = [s for s in c.signals() if s["sig_id"] == sid][0]["strength"]
time.sleep(1.5)  # tick=0.5s, 约2-3次衰减
sigs = c.signals()
s1 = next((s["strength"] for s in sigs if s["sig_id"] == sid), 0)
check("T3 信号衰减", s1 < s0, f"{s0:.2f}→{s1:.2f}")

# T4 死信号清除(强度低于阈值一半会被物理删除)
sid2 = c.emit("die_test", {}, decay=0.99, source="test")["sig_id"]
time.sleep(2.0)
sigs = c.signals()
gone = not any(s["sig_id"] == sid2 for s in sigs)
check("T4 死信号物理清除", gone)

# T5 kv读写
c.kv_set("test/key", {"a": 1, "b": [1, 2]})
v = c.kv_get("test/key")
check("T5 kv读写", v == {"a": 1, "b": [1, 2]}, str(v))

# T6 kv覆盖+revision单调
r1 = c.kv_set("test/key", {"v": 2})["revision"]
r2 = c.kv_set("test/key", {"v": 3})["revision"]
check("T6 revision单调递增", r2 > r1, f"{r1}→{r2}")

# T7 焦点放大(attention里focus_task匹配×1.5)
sid3 = c.emit("focus_test", {"task_id": "task_A"}, decay=0, source="test")["sig_id"]
c.kv_set("focus", {"task_id": "task_A"})
sigs = c.attention(focus_task="task_A")
st = next((s["strength"] for s in sigs if s["sig_id"] == sid3), 0)
check("T7 焦点放大×1.5", abs(st - 1.5) < 0.01, f"strength={st}")

# T8 drop信号
c.drop(sid3)
sigs = c.signals()
gone = not any(s["sig_id"] == sid3 for s in sigs)
check("T8 drop删除信号", gone)

# T9 并发读写(10线程×20轮, 零异常+零锁死)
import threading
errs = []
def worker(wid):
    try:
        cc = fresh_client()
        for i in range(20):
            cc.emit("concurrent", {"w": wid, "i": i}, decay=0.5, source=f"w{wid}")
            cc.kv_get("test/key")
            cc.kv_set(f"conc/{wid}", i)
        cc.close()
    except Exception as e:
        errs.append(f"w{wid}: {e}")
threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
t0 = time.time()
for t in threads: t.start()
for t in threads: t.join()
dt = time.time() - t0
check("T9 10线程并发零异常", len(errs) == 0, "; ".join(errs[:2]))
check("T10 并发吞吐≥5轮/秒", 200/dt >= 5, f"{200/dt:.1f}轮/秒({dt:.1f}s)")

c.close()
print(f"\n{'='*40}")
p = sum(1 for _, ok in results if ok)
print(f"结果: {p}/{len(results)} 通过")
sys.exit(0 if p == len(results) else 1)
