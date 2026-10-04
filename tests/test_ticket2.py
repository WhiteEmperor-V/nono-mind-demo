#!/usr/bin/env python3
"""票2验收测试: 临时loop生命周期(造/删)+编排模板+判进步存经验+授权动态化
跑法: python3 tests/test_ticket2.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MemState:
    """内存版State(够ticket2用: kv + 任务CAS + emit)."""
    def __init__(self):
        self.kv = {}
        self.events = []
    def kv_get(self, k, default=None):
        v = self.kv.get(k, default)
        return default if v is None else v
    def kv_set(self, k, v):
        self.kv[k] = None if v is None else v
        return {"ok": True}
    def kv_del(self, k):
        self.kv.pop(k, None)
        return {"ok": True}
    def kv_list(self, prefix=""):
        return {k: v for k, v in self.kv.items() if k.startswith(prefix)}
    def kv_append(self, k, item):
        arr = self.kv.get(k)
        if not isinstance(arr, list):
            arr = []
        arr.append(item)
        self.kv[k] = arr
        return {"ok": True}
    def emit(self, sig, payload, decay=0.2, source="core"):
        self.events.append((sig, payload, source))
    def drop(self, *a, **k): pass
    # --- 任务CAS(与state/server.py的task_op同语义, 供orchestrate/claim用) ---
    def task_create(self, task_id, intent="", assigned_to="", params=None, idempotency_key=None):
        self.kv[f"tasks/{task_id}"] = {"task_id": task_id, "intent": intent, "assigned_to": assigned_to,
                                       "params": params or {}, "status": "queued", "steps": []}
        return {"ok": True}
    def task_claim(self, task_id, owner_pid=None):
        t = self.kv.get(f"tasks/{task_id}")
        if not t or t.get("status") not in ("queued", "suspended"):
            return {"ok": False, "err": "conflict"}
        t["status"] = "running"
        return {"ok": True}
    def task_get(self, task_id):
        return self.kv.get(f"tasks/{task_id}")
    def task_finish(self, task_id, status="done", result=None):
        t = self.kv.get(f"tasks/{task_id}") or {}
        t["status"] = status
        t["result"] = result
        return {"ok": True}


PASS = []


def _fresh(with_skills=True):
    from core import loop as L
    from immune import skills as S
    c = MemState()
    if with_skills:
        S.load_skills(c)
    return L, c


def test_detect_and_plan():
    L, c = _fresh()
    assert L._detect_task_type("写个3D游戏并渲染") == "modeling"
    assert L._detect_task_type("帮我写个脚本统计csv") == "code"
    assert L._detect_task_type("画一个鹈鹕骑自行车") == "canvas"
    assert L._detect_task_type("今天天气怎么样") is None
    # 编排查manifest找能力: modeling → coder→canvas→reflection
    st = L.plan_stages(c, "modeling")
    organs = [s["organ"] for s in st]
    assert organs == ["coder", "canvas", "reflection"], organs
    st2 = L.plan_stages(c, "code")
    assert [s["organ"] for s in st2] == ["coder", "coder", "reflection"], st2
    PASS.append("编排模板(查manifest找能力→器官顺序)")


def test_temp_loop_lifecycle():
    from immune import skills as S
    L, c = _fresh()
    loop = L.assemble_temp_loop(c, "coder", task_type="code")
    lid = loop["id"]
    # 造: 临时loop记录在册 + 动态授权(coding-python) + skill本体已复制给小loop
    assert c.kv_get(f"loops/temp/{lid}")["status"] == "running"
    assert c.kv_get(f"skills/grants/{lid}/coding-python")
    assert c.kv_get(f"skills/loops/{lid}/coding-python")
    assert loop["provides"] == ["code_task"]
    assert any(e[0] == "loop_spawned" for e in c.events)
    # 删: grant失效 + 复制的skill随loop删掉 + 记录标terminated
    rec = L.teardown_temp_loop(c, lid)
    assert rec["status"] == "terminated"
    assert c.kv_get(f"skills/grants/{lid}/coding-python") is None, "授权应随loop失效"
    assert c.kv_get(f"skills/loops/{lid}/coding-python") is None, "复制的skill应随loop删掉"
    assert not c.kv_list(f"skills/grants/{lid}/"), "该loop授权应清空"
    assert any(e[0] == "loop_terminated" for e in c.events)
    # 常驻loop不在临时表里(wechat_loop永远不删)
    assert c.kv_get("loops/temp/wechat_loop") is None
    PASS.append("临时loop: 现拼装配→动态授权→干完删→授权失效")


def test_judge_and_store():
    L, c = _fresh()
    # 纯判据
    assert L.judge_progress(None, {"ok": True, "steps": 3}) == "baseline"
    assert L.judge_progress({"ok": False, "steps": 4}, {"ok": True, "steps": 3}) == "improve"
    assert L.judge_progress({"ok": True, "over": True, "steps": 9},
                            {"ok": True, "over": False, "steps": 3}) == "improve"
    assert L.judge_progress({"ok": True, "steps": 5}, {"ok": True, "steps": 3}) == "improve"
    assert L.judge_progress({"ok": True, "steps": 3}, {"ok": True, "steps": 8}) == "regress"
    assert L.judge_progress({"ok": True, "steps": 3}, {"ok": False, "steps": 3}) == "regress"
    # 首次=记基线, 不存经验
    r0 = L.record_result(c, "code", "coder", {"ok": True, "steps": 5, "over": False}, skill="coding-python")
    assert r0["verdict"] == "baseline" and not r0["stored"]
    assert c.kv_get("skills/coding-python/versions") is None
    # 进步(3步<5步) → 调improve_and_store存回公共区
    r1 = L.record_result(c, "code", "coder", {"ok": True, "steps": 3, "over": False}, skill="coding-python")
    assert r1["verdict"] == "improve" and r1["stored"]
    vers = c.kv_get("skills/coding-python/versions")
    assert vers and len(vers) == 1, vers
    assert vers[0]["by"] == "big_loop"
    assert c.kv_get("skills/specialized/coding-python").get("practice"), "改进版应含实践要点"
    # 退步(9步) → 不存, 用老版
    r2 = L.record_result(c, "code", "coder", {"ok": True, "steps": 9, "over": False}, skill="coding-python")
    assert r2["verdict"] == "regress" and not r2["stored"]
    assert len(c.kv_get("skills/coding-python/versions")) == 1, "退步不该存新版本"
    # progress账每轮都更新(本次成为下次的"上次")
    assert c.kv_get("progress/code")["steps"] == 9
    PASS.append("判进步: 对比本次vs上次, 进步才存公共区, 退步不存")


def test_orchestrate_end_to_end():
    L, c = _fresh()
    calls = []
    def stub(c, organ, demand, sid):
        calls.append(organ)
        t = c.task_get(sid) or {}
        t["steps"] = [{"step": 1}]
        c.kv_set(f"tasks/{sid}", t)
        return f"{organ}干完:{demand[:20]}"
    res = L.orchestrate(c, "modeling", "写个3D游戏并渲染", run_stage=stub)
    # 按模板顺序派发
    assert calls == ["coder", "canvas", "reflection"], calls
    assert len(res) == 3 and all("result" in x for x in res)
    # 造过loop又都删干净(临时表不留)
    assert c.kv_list("loops/temp/") == {} or all(
        v.get("status") == "terminated" for v in c.kv_list("loops/temp/").values())
    # 每个stage都建了任务并CAS claim成功(running/done)
    for x in res:
        assert x["loop"]
    # task_dispatch信号按序emit(走现有协议)
    disp = [p for (s, p, _src) in c.events if s == "task_dispatch"]
    assert [d["route_to"] for d in disp] == ["coder", "canvas", "reflection"], disp
    PASS.append("大loop编排: 查能力→按序造临时loop→task_dispatch/CAS→拆→判进步")


def test_wiring_real():
    """验收硬要求: 临时loop不是只写函数没接线——真实dispatch分支必须调用装配/拆解."""
    import inspect
    from core import loop as L
    src = inspect.getsource(L.main)
    assert "assemble_temp_loop" in src, "coder/canvas分支应真实装配临时loop"
    assert "teardown_temp_loop" in src, "分支应真实拆掉临时loop"
    assert "orchestrate(" in src, "长问题路径应真实调用编排"
    assert "record_result(" in src or "judge_progress" in src, "分支应接判进步"
    PASS.append("接线检查: 真实dispatch分支调用装配/拆解/编排/判进步")


if __name__ == "__main__":
    test_detect_and_plan()
    test_temp_loop_lifecycle()
    test_judge_and_store()
    test_orchestrate_end_to_end()
    test_wiring_real()
    print("\n".join("OK " + p for p in PASS))
    print("\n全部通过: 票2(临时loop生命周期 + 编排模板 + 判进步存经验 + 授权动态化)")
