#!/usr/bin/env python3
"""票1验收: skills分级 + 动态授权 + 复制/改进存回 + 路径回归(CWD无关)
跑法: python3 tests/test_skills.py
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MemState:
    def __init__(self):
        self.kv = {}; self.events = []
    def kv_set(self, k, v): self.kv[k] = v
    def kv_get(self, k, default=None): return self.kv.get(k, default)
    def kv_list(self, prefix=""):
        return {k: v for k, v in self.kv.items() if k.startswith(prefix)}
    def kv_del(self, k): self.kv.pop(k, None)
    def emit(self, sig, payload, decay=0.2, source="test"):
        self.events.append((sig, payload, source))


def test_load_finds_skills():
    from immune import skills as S
    c = MemState()
    ng, ns = S.load_skills(c)
    assert ng == 2, f"general应2, 实际{ng}"
    assert ns == 3, f"specialized应3, 实际{ns}"
    assert c.kv_get("skills/general/state-read"), "skill本体应存进公共区"
    print(f"OK load_skills 扫到 general={ng} specialized={ns}")


def test_load_is_cwd_independent():
    """回归: 原来'扫不到skill'是路径问题——从别的目录跑也必须扫到."""
    from immune import skills as S
    assert os.path.isabs(S.SKILLS_DIR), "SKILLS_DIR必须是绝对路径"
    old = os.getcwd()
    d = tempfile.mkdtemp()
    try:
        os.chdir(d)
        c = MemState()
        ng, ns = S.load_skills(c)
        assert (ng, ns) == (2, 3), f"换CWD后扫不到skill了: {(ng, ns)}"
    finally:
        os.chdir(old)
    print("OK 路径锚定仓库根(与CWD无关), 换目录也扫得到")


def test_lazy_bootstrap_on_first_use():
    """回归: 没人显式load时, 小loop第一次查权限也应懒加载到skill."""
    from immune import skills as S
    c = MemState()  # 全新state, 没有 _index
    assert S.can_use(c, "coder-1", "state-read"), "首次查权限应自动load出general skill"
    assert not S.can_use(c, "coder-1", "coding-python"), "未授权的专精skill不可用"
    print("OK 首次查权限懒加载(小loop用前查得到)")


def test_grant_deny_and_visible():
    from immune import skills as S
    c = MemState(); S.load_skills(c)
    assert S.can_use(c, "anyone", "self-report")          # general恒可用
    assert not S.can_use(c, "coder-1", "coding-python")   # 专精未授权
    assert S.grant(c, "coder-1", "coding-python", "派活")
    assert S.can_use(c, "coder-1", "coding-python")       # 动态授权后可用
    assert not S.can_use(c, "coder-2", "coding-python")   # 授权跟loop走, 别的loop不行
    vs = S.visible_skills(c, "coder-1")
    assert "state-read" in vs and "coding-python" in vs and "quant-trading" not in vs
    print("OK 动态授权(general全开/专精按loop授权)")


def test_copy_and_improve():
    from immune import skills as S
    c = MemState(); S.load_skills(c); S.grant(c, "coder-1", "coding-python")
    assert S.copy_to(c, "coder-1", "coding-python"), "应能把skill复制一份给小loop"
    body = c.kv_get("skills/loops/coder-1/coding-python")["body"]
    assert body["name"] == "coding-python"
    improved = dict(body); improved["note"] = "加了边界检查"
    assert S.improve_and_store(c, "coder-1", "coding-python", improved, note="进步")
    assert len(c.kv_get("skills/coding-python/versions")) == 1
    # 存回公共区(本体更新), 原公共区仍在
    assert c.kv_get("skills/specialized/coding-python")["note"] == "加了边界检查"
    print("OK 复制给小loop + 改进存回公共区")


if __name__ == "__main__":
    test_load_finds_skills()
    test_load_is_cwd_independent()
    test_lazy_bootstrap_on_first_use()
    test_grant_deny_and_visible()
    test_copy_and_improve()
    print("\n全部通过: skills分级+动态授权+路径回归")
