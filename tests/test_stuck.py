import sys
sys.path.insert(0, "/root/nono-mind")
from immune.stuck_detector import StuckDetector

s = StuckDetector()
obs_bad = {"ok": False, "stdout": "", "stderr": "NameError: x not defined", "returncode": 1}
for _ in range(3):
    a = s.observe("print(x)", obs_bad)
assert a == "repeat_error", f"期望repeat_error, 得到{a}"
assert "换思路" in s.nudge(a)

s2 = StuckDetector()
for _ in range(3):
    a2 = s2.observe("print('same')", {"ok": True, "stdout": "same", "stderr": "", "returncode": 0})
assert a2 == "repeat_action", f"期望repeat_action, 得到{a2}"

s3 = StuckDetector()
for _ in range(4):
    a3 = s3.observe("import os", {"ok": True, "stdout": "", "stderr": "", "returncode": 0})
assert a3 == "empty", f"期望empty, 得到{a3}"

s4 = StuckDetector()
for i in range(6):
    a4 = s4.observe(f"print({i})", {"ok": True, "stdout": str(i), "stderr": "", "returncode": 0})
    assert a4 == "ok", f"正常流被误判: {a4} @ step{i}"

# no_progress: 代码不同但观察完全不变(LLM在空转换花样)
s5 = StuckDetector()
for i in range(4):
    a5 = s5.observe(f"print('fixed{i}')", {"ok": True, "stdout": "fixed output", "stderr": "", "returncode": 0})
assert a5 == "no_progress", f"期望no_progress, 得到{a5}"

print("StuckDetector 5项自检全过")
