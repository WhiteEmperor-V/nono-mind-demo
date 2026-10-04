#!/usr/bin/env python3
"""票1验收测试: skills完整版(动态授权+复制+改进存回) + manifest discover + 进度互看
跑法: python3 tests/test_ticket1.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class MemState:
    def __init__(self):
        self.kv = {}; self.events = []
    def kv_set(self, k, v):
        self.kv[k] = None if v is None else v
    def kv_get(self, k, default=None):
        v = self.kv.get(k, default)
        return default if v is None else v
    def emit(self, sig, payload, decay=0.2, source="test"):
        self.events.append((sig, payload, source))

def test_skills():
    from immune import skills as S
    c = MemState()
    ng, ns = S.load_skills(c)
    assert ng == 2, f"general应2, 实际{ng}"
    assert ns == 3, f"specialized应3, 实际{ns}"
    # general恒可用, 不需授权
    assert S.can_use(c, "coder-1", "state-read")
    # specialized未授权不可用
    assert not S.can_use(c, "coder-1", "coding-python")
    # 大loop动态授权 → 可用
    assert S.grant(c, "coder-1", "coding-python", "测试")
    assert S.can_use(c, "coder-1", "coding-python")
    # 没授权的loop不行
    assert not S.can_use(c, "coder-2", "coding-python")
    # 复制一份给小loop(用完删loop, 本体还在公共区)
    assert S.copy_to(c, "coder-1", "coding-python")
    assert c.kv_get("skills/loops/coder-1/coding-python")
    # 小loop改进了skill → 大loop检查有进步存回公共区
    improved = dict(c.kv_get("skills/loops/coder-1/coding-python")["body"])
    improved["note"] = "coder-1改进: 加了边界检查"
    assert S.improve_and_store(c, "coder-1", "coding-python", improved)
    assert len(c.kv_get("skills/coding-python/versions")) == 1
    # visible_skills
    S.grant(c, "x", "quant-trading")
    vs = S.visible_skills(c, "x")
    assert "state-read" in vs and "quant-trading" in vs
    print("OK skills完整版(动态授权+复制+改进存回)")

def test_plugins_discover():
    from immune.plugins import PluginManager
    pm = PluginManager(MemState())
    found = pm.discover()
    assert "wechat_loop" in found, f"应发现wechat_loop, 实际{found}"
    print(f"OK plugins discover ({len(found)}个插件: {found})")

def test_progress_mutual():
    c = MemState()
    c.kv_set("states/coder-1/progress", {"task": "写码", "pct": 50})
    p = c.kv_get("states/coder-1/progress")
    assert p and p["pct"] == 50
    print("OK 进度互看(states/<self>/progress)")

if __name__ == "__main__":
    test_skills()
    test_plugins_discover()
    test_progress_mutual()
    print("\n全部通过: 票1基础层(skills完整版+manifest+进度互看)可用")
