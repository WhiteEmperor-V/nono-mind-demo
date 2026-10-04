#!/usr/bin/env python3
"""六地基验收: BDI suspend/检索/评价系统"""
import sys, time
sys.path.insert(0, "/root/nono-mind")
from state.server import StateClient
c = StateClient()
# BDI suspend
tid = f"bdi_{int(time.time())}"
c.task_create(tid, intent="code_task", assigned_to="coder", params={"demand": "测试重拾"})
c.task_claim(tid)
c.task_suspend(tid, "测试挂起")
t = c.task_get(tid)
print(f"1. BDI挂起: {t['status']} (原因: {t.get('suspend_reason')}) ✓")
# 三因子检索
from immune import retrieval as rt
hits = rt.search(c, "统计 行数 脚本", top_k=2)
print(f"2. 三因子检索: {len(hits)}条命中")
for h in hits:
    print(f"   [{h['key']}] score={h['score']} {h['content'][:60]}")
# 评价系统
from immune.evals import self_eval, ability_profile
c.task_create("eval_test", intent="test")
self_eval(c, "eval_test", "coder", [{"ok": True}, {"ok": True}], "测试评价")
p = ability_profile(c, "coder")
print(f"3. 评价系统: {p}")
print("=== 六地基验收完成 ===")
c.close()
