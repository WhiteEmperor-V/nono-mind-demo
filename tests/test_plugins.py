#!/usr/bin/env python3
"""票1验收: 器官manifest→能力注册 + 多实例 + 进度互看 + 公共判断账可读
跑法: python3 tests/test_plugins.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ORGANS = ["dialogue", "reflection", "canvas", "coder", "wellness"]


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


def test_discover_manifests():
    from immune.plugins import discover_manifests
    found = discover_manifests()
    for o in ORGANS:
        assert o in found, f"应发现器官manifest: {o}, 实际{list(found)}"
    assert "wechat_loop" in found
    print(f"OK 扫描plugins/*/manifest.yaml: {sorted(found)}")


def test_capabilities_from_manifest_valid():
    from immune.plugins import capabilities_from_manifest, provides_from_manifest
    from immune import capabilities as caps
    for o in ORGANS:
        cl = capabilities_from_manifest(o)
        assert cl, f"{o}的manifest应声明至少1条能力"
        assert provides_from_manifest(o), f"{o}的插件应声明provides"
        for cap in cl:
            ok, err = caps.validate_capability(cap)
            assert ok, f"{o}/{cap.get('name')} 能力声明非法: {err}"
            assert cap["organ"] == o
    print("OK manifest能力声明合法(且跟插件provides对齐)")


def test_register_from_manifests():
    from immune.plugins import register_capabilities_from_manifests
    c = MemState()
    ok, total = register_capabilities_from_manifests(c)
    # ponytail: 数量随新造的插件(如nas_disk)增加, 这里只断言核心器官仍在 + 数量合理
    coder = c.kv_get("capabilities/coder")
    assert coder and any(x["name"] == "code_task" for x in coder)
    assert total >= 5, f"至少5条内置能力, 实际{total}"
    assert ok == total, f"全部应通过校验, 实际{ok}/{total}"
    print(f"OK 扫描注册能力进capabilities表 ({ok}/{total})")


def test_multi_instance():
    from immune.plugins import instances_from_manifest
    assert instances_from_manifest("coder") == ["coder-1", "coder-2"], "coder应可开2实例"
    assert instances_from_manifest("dialogue") == ["dialogue"], "单实例器官就一份"
    print("OK 按manifest动态开多实例(coder→coder-1/coder-2)")


def test_requires_skills():
    from immune.plugins import requires_skills_from_manifest
    assert "coding-python" in requires_skills_from_manifest("coder")
    assert "state-read" in requires_skills_from_manifest("dialogue")
    print("OK manifest声明所需skill(装配工作loop时带上)")


def test_progress_mutual_visibility():
    from immune.plugins import write_progress, read_progress
    c = MemState()
    write_progress(c, "coder-1", "写码", pct=50)
    # 别的loop直接读
    p = read_progress(c, "coder-1")
    assert p and p["pct"] == 50 and "写码" in p["task"]
    assert "states/coder-1/progress" in c.kv
    print("OK 进度互看(states/<自己>/progress, 零新协议)")


def test_track_progress_decorator():
    from immune.plugins import track_progress
    c = MemState()

    @track_progress("coder-9", lambda demand, c: demand)
    def work(demand, c):
        return "done"

    assert work("整理图片", c) == "done"
    p = c.kv_get("states/coder-9/progress")
    assert p["task"] == "整理图片" and p["pct"] == 100
    print("OK 器官入口自动写进度(track_progress)")


def test_judgments_public_layer():
    from immune.plugins import read_judgments
    from organs.judgments import write_judgment
    c = MemState()
    write_judgment(c, "切到cpa主脑", why="scnet挂", invalid_if="scnet恢复")
    js = read_judgments(c)
    assert len(js) == 1 and js[0]["status"] == "active" and js[0]["text"] == "切到cpa主脑"
    print("OK 公共记忆层: judgments判断账所有loop可读")


def test_plugin_manager_discover_still_ok():
    from immune.plugins import PluginManager
    pm = PluginManager(MemState())
    assert "wechat_loop" in pm.discover()
    print("OK 旧插件管理器discover不回归")


if __name__ == "__main__":
    test_discover_manifests()
    test_capabilities_from_manifest_valid()
    test_register_from_manifests()
    test_multi_instance()
    test_requires_skills()
    test_progress_mutual_visibility()
    test_track_progress_decorator()
    test_judgments_public_layer()
    test_plugin_manager_discover_still_ok()
    print("\n全部通过: manifest能力注册+多实例+进度互看+judgments公共层")
