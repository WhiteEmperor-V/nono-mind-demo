#!/usr/bin/env python3
"""意识核主循环 v2: 接入State服务(C/S), 常驻模式
扫描细胞外液 → 取最强信号 → 分派器官 → checkpoint
wikis/nono/core-architecture-v1.md §15.1
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from state.server import StateClient
from immune.router import ROUTE_TABLE, KEYWORD_ESCALATE
from organs import dialogue
from organs import reflection
from state.model import PRIORITY

IDLE_REFLECT_SECONDS = float(os.environ.get("IDLE_REFLECT_SECONDS", 600))
IDLE_REFLECT_MIN_INTERVAL = float(os.environ.get("IDLE_REFLECT_MIN_INTERVAL", 1800))

def maybe_idle_reflect(c, last_active, now=None):
    """闲时反思: 连续空闲超阈值 → emit reflection_trigger(轻反思)
    防风暴: 两次空闲反思间隔至少 IDLE_REFLECT_MIN_INTERVAL(默认30分钟), 记State kv
    返回 (新的last_active, 是否触发)"""
    now = now if now is not None else time.time()
    if now - last_active < IDLE_REFLECT_SECONDS:
        return last_active, False
    last = (c.kv_get("last_idle_reflection") or {}).get("at", 0)
    if now - last < IDLE_REFLECT_MIN_INTERVAL:
        return last_active, False
    idle_for = int(now - last_active)
    c.emit("reflection_trigger",
           {"reason": "idle", "idle_for_seconds": idle_for,
            "route_to": "reflection", "priority": 3},
           decay=0.05, source="core")
    c.kv_set("last_idle_reflection", {"at": now, "idle_for_seconds": idle_for})
    return now, True

def route_to(payload):
    p = json.loads(payload) if isinstance(payload, str) else payload
    return p.get("route_to")

# ---------- 判断账 · 挂点A: 架构级决策留痕 ----------
def note_decision(c, text, why="", invalid_if="", probe="", probe_arg=None):
    """挂点A: 决策会影响以后行为时, 多写一条到 judgments/. 判据(调用方把关):
    决策动了 llm_client通道 / state.db配置 / 放弃了一个可选方案 之一, 才写; 普通闲聊不写(防爆炸).
    ponytail: 失败不阻断主循环."""
    try:
        from organs.judgments import write_judgment
        return write_judgment(c, text, why=why, invalid_if=invalid_if,
                              probe=probe, probe_arg=probe_arg)
    except Exception as e:
        print(f"[意识核] 写judgment失败(不阻断): {e}")
        return None

# ---------- Phase2: 能力注册表 + LLM现场匹配(Q1) ----------
import re as _re
from immune import capabilities as _caps

_MATCH_RE = _re.compile(r"\{[\s\S]*\}")

def _extract_text(payload) -> str:
    """从信号payload里取出主人原话"""
    p = json.loads(payload) if isinstance(payload, str) else payload
    d = p.get("data", p)
    if isinstance(d, dict):
        return str(d.get("text", ""))
    return str(d)

def _llm_match(c, text: str) -> dict:
    """大loop现场匹配: 原话+历史+能力注册表 → LLM输出 {organ, params, reason} 或 {organ: null}.
    返回 {"organ": str|None, "params": dict, "reason": str}. 异常时返回 organ=None(走回退)."""
    first = _match_once(c, text)
    # GLM叙述癖兜底: 解析失败→gpt-5.6-sol强模型重试一轮
    if first.get("_parse_fail"):
        retry = _strong_match(text)
        if retry is not None:
            return retry
    return first

def _strong_match(text: str):
    """agnes强模型匹配(Responses链已切chat链). 解析失败时兜底重试一轮, 失败返回None. (2026-09-30删apikey.fun, 改走llm_client.chat)"""
    try:
        import json as _json
        from llm_client import chat
        registry = _caps.to_prompt(_STATE_HOLDER["c"])
        prompt = ("你是agent调度器。读用户消息和能力清单, 只输出JSON: "
                  '能干输出{"organ":"<organ>","params":{"demand":"..."},"reason":"..."}; '
                  '不能干输出{"organ":null,"reason":"..."}。'
                  "organ必须从清单选。禁止输出JSON以外的任何文字。\n\n"
                  "能力清单:\n" + registry + "\n\n用户消息: " + text)
        txt = str(chat([{"role": "user", "content": prompt}], max_tokens=2000, timeout=60))
        m = _MATCH_RE.search(txt)
        if not m:
            return None
        out = _json.loads(m.group(0))
        organ = out.get("organ")
        if organ:
            return {"organ": organ.split("/")[0].strip(), "params": out.get("params") or {},
                    "reason": str(out.get("reason", ""))[:200] + "(强模型)"}
        return {"organ": None, "params": {}, "reason": str(out.get("reason", ""))[:200] + "(强模型)"}
    except Exception as e:
        print(f"[意识核] 强模型匹配失败: {e}")
        return None

# 强模型重试时需要StateClient引用(handle传进来太绕, 用holder)
_STATE_HOLDER = {}
POISON_CNT = {}   # 毒信号连续失败计数(sig_id -> 次数)

def _match_once(c, text: str) -> dict:
    """单次GLM匹配(原_llm_match主体)"""
    _STATE_HOLDER["c"] = c
    from llm_client import chat
    registry = _caps.to_prompt(c)
    hist = c.kv_get("dialogue_history/" + time.strftime("%Y-%m-%d")) or []
    hist_lines = []
    for x in hist[-10:]:
        if isinstance(x, dict) and x.get("role") and x.get("content"):
            hist_lines.append(f"[{x['role']}] {str(x['content'])[:120]}")
    hist_txt = "\n".join(hist_lines)
    # 2026-09-30主人定案(判断力): 掂量≠认输. 掂完发现"没现成能力"不停在那,
    # 交coder想办法达成(造工具/查方案/降级), 交付才算完成. 不瞎答应但也不停在"我不会".
    sys_p = ("你是新身体的大脑(调度中心). 读了主人的话和下面的能力清单后, "
             "判断应该派给哪个器官执行.\n"
             "【掂量铁律(主人9/30定)】先掂量'这事清单里有没有现成能力': "
             "有现成的→派对应器官; 没有现成的/勉强→交给coder(编程器官), 让它自己想办法达成"
             "(造工具/查方案/降级组合现有能力), 别停在'我不会'. 闲聊/日常→dialogue; "
             "指代(那张图/刚才的)从对话历史里还原. 只输出JSON, 不输出别的.\n"
             "能力清单:\n" + registry +
             "\n\n输出格式二选一:\n"
             '能干: {"organ":"<器官名>","params":{"demand":"<完整任务描述>"},"reason":"<一句话>"}\n'
             '没有现成能力: {"organ":"coder","params":{"demand":"<完整任务描述>"},"reason":"<缺什么能力, 要coder想办法达成>"}')
    msgs = [{"role": "system", "content": sys_p}]
    if hist_txt:
        msgs.append({"role": "system", "content": "最近对话:\n" + hist_txt})
    msgs.append({"role": "user", "content": text})
    raw = chat(msgs, max_tokens=300)
    m = _MATCH_RE.search(raw)
    if not m:
        return {"organ": None, "params": {}, "reason": "LLM输出无法解析", "_parse_fail": True}
    try:
        out = json.loads(m.group(0))
    except Exception:
        return {"organ": None, "params": {}, "reason": "LLM输出JSON无效", "_parse_fail": True}
    organ = out.get("organ")
    if organ and not isinstance(organ, str):
        organ = None
    return {"organ": (organ or None), "params": out.get("params") or {}, "reason": str(out.get("reason", ""))[:200]}

def _guess_difficulty(reason: str, demand: str = "") -> str:
    """从匹配理由+任务原文猜任务难度(Ponytail: 不加LLM调用, 关键词规则够用)
    2026-09-15教训: 调研/对比/多源任务被误判simple(4步)两次预算耗尽——调研类天然多步"""
    text = (reason + " " + demand).lower()
    # 调研/对比/多源类: 涉及多来源阅读整合, 起步就是medium以上
    if any(w in text for w in ("调研", "对比", "方案", "推荐", "研究", "搜索", "收集", "了解", "调研报告")):
        return "medium"
    if any(w in text for w in ("简单", "统计", "列出", "simple")) and "对比" not in text and "调研" not in text:
        return "simple"
    if any(w in text for w in ("复杂", "重构", "多步", "调试", "爬", "hard", "设计")):
        return "hard"
    return "medium"


# ---------- 票2: 大loop编排(静态模板) + 临时loop生命周期 + 判进步存经验 ----------
# 模板 = 建议顺序(能力名), 强脑子可用 orchestrate(order=...) override; 不写死.
# 一句话: 长问题按manifest查能力→现拼临时loop→按序派发(走现有task_dispatch/CAS)→干完删loop→判进步存经验.
TASK_TEMPLATES = {
    "code":     ["code_task", "code_task", "self_check"],      # 写码→跑测→复盘
    "modeling": ["code_task", "canvas_create", "self_check"],  # 建模类: coder→canvas→reflection
    "canvas":   ["canvas_create", "self_check"],
    "mixed":    ["code_task", "canvas_create", "self_check"],  # 混合: 先写码再画图再复盘
}

_TASK_TYPE_KW = (
    # 混合任务优先(写码+画图): 先干码再画图, 不整个吞进canvas
    ("mixed",    ("写个函数", "写个代码", "写个程序", "写码", "代码", "脚本", "编程")),
    ("modeling", ("3d", "3D", "建模", "渲染", "threejs", "three.js", "游戏", "动画")),
    ("canvas",   ("画", "页面", "可视化", "html", "图表", "做张图")),
    ("code",     ("代码", "脚本", "编程", "调试", "重构", "爬", "统计", "整理", "写个", "自动化")),
)
_MIXED_CANVAS_KW = ("画", "图", "可视化", "页面", "html", "图表")

# 2026-09-30主人定案: 入口放宽——"要我去办事"的话(没编程词)也判任务, 派coder走第一性原理
# 治"帮我看看NAS/想办法/弄一下"被当闲聊漏过去的入口病(不局限带编程词的活)
_ACTION_INTENT_KW = (
    "帮我看看", "帮我查", "帮我弄", "帮我解决", "想办法", "搞定", "弄一下",
    "解决", "处理一下", "搞一下", "清理", "腾", "删除", "帮我处理",
)

def _detect_task_type(text: str):
    """原话→任务类型(关键词规则, 不调LLM).
    混合任务(写码+画图)→mixed(拆成coder→canvas→reflection三段).
    2026-09-30主人定案: "要我去办事"的话(帮我看看/想办法/弄一下, 没编程词)也判code任务,
    派coder走第一性原理掂本质(自己安全范围内做完, 别推回主人). 认不出返回None."""
    t = str(text or "")
    has_code = any(w in t for w in ("函数", "代码", "脚本", "编程", "写码"))
    has_canvas = any(w in t for w in _MIXED_CANVAS_KW)
    if has_code and has_canvas:
        return "mixed"
    for tt, kws in _TASK_TYPE_KW:
        if tt == "mixed":
            continue
        if any(w in t for w in kws):
            return tt
    # 入口放宽: 主人要我"去办事"(帮我看看/想办法/弄一下)→ 判code任务, 走coder第一性原理
    # 2026-10-02主人定案"以她为中心, 让她自己造自己": 拆掉这层替她做主的词表兜底——
    # "要我办事"的意图该她脑子(dialogue)自己掂出来, 不靠我外部词表判. 词表只留明确带任务词的, 其余全透传dialogue.
    return None

def plan_stages(c, task_type):
    """查manifest把模板里的能力名解析成器官顺序. 返回[{organ,cap}...]. 未注册的能力跳过(不硬报错)."""
    from immune.plugins import organ_for_provides, register_capabilities_from_manifests
    register_capabilities_from_manifests(c)          # 复用票1: 保证能力表就绪
    stages = []
    for cap in TASK_TEMPLATES.get(task_type, []):
        organ = organ_for_provides(cap)
        if organ:
            stages.append({"organ": organ, "cap": cap})
    return stages

def assemble_temp_loop(c, organ, task_type=""):
    """按manifest现拼一个临时loop: 读provides/requires_skills装配, 动态授权(用完即失效)+复制skill本体.
    返回loop记录. 常驻loop(continuous)不走这里(只激活/熄火)."""
    from immune.plugins import (provides_from_manifest, requires_skills_from_manifest,
                                register_capabilities_from_manifests)
    from immune import skills as _sk
    register_capabilities_from_manifests(c)
    provides = provides_from_manifest(organ)
    need = requires_skills_from_manifest(organ)
    lid = f"temp-{organ}-{time.time_ns() % 10 ** 9}"
    for sk in need:                       # 大loop派活时现场grant(授权动态化收尾)
        _sk.grant(c, lid, sk, reason=f"临时loop:{task_type or organ}")
        _sk.copy_to(c, lid, sk)           # skill本体复制给小loop(大loop本体仍在公共区)
    rec = {"id": lid, "organ": organ, "provides": provides, "skills": need,
           "task_type": task_type, "status": "running", "at": time.time()}
    c.kv_set(f"loops/temp/{lid}", rec)
    c.emit("loop_spawned", {"loop": lid, "organ": organ, "skills": need},
           decay=0.2, source="core")
    return rec

def teardown_temp_loop(c, loop_id, status="terminated"):
    """干完活撤掉临时loop: 删grant(授权跟着失效)+删复制的skill+标状态. 复用插件子进程的terminate语义. 返回记录."""
    from immune import skills as _sk
    rec = c.kv_get(f"loops/temp/{loop_id}") or {"id": loop_id, "organ": loop_id, "skills": []}
    try:
        rec["revoked"] = _sk.revoke(c, loop_id)
    except Exception:
        rec["revoked"] = 0
    for sk in rec.get("skills") or []:
        c.kv_del(f"skills/loops/{loop_id}/{sk}")
    rec["status"] = status
    rec["ended_at"] = time.time()
    c.kv_set(f"loops/temp/{loop_id}", rec)
    c.emit("loop_terminated", {"loop": loop_id, "organ": rec.get("organ"), "status": status},
           decay=0.2, source="core")
    return rec

def judge_progress(prev, cur):
    """判进步(纯函数, 大loop拿"本次"比"上次"同类结果, 不让小loop自夸):
    进步='improve' | 退步='regress' | 持平='flat' | 首次='baseline'.
    判据(任一即进步): 上次失败这次成功; 上次超时/卡死这次没有; 都成功且这次步数更少。"""
    if not prev:
        return "baseline"
    if cur.get("ok") and not prev.get("ok"):
        return "improve"
    if prev.get("over") and not cur.get("over"):
        return "improve"
    if cur.get("ok") and prev.get("ok"):
        if cur.get("steps", 0) < prev.get("steps", 0):
            return "improve"
        if cur.get("steps", 0) > prev.get("steps", 0):
            return "regress"
        return "flat"
    if not cur.get("ok") and prev.get("ok"):
        return "regress"
    return "flat"

def _metrics_from_task(c, task_id, result):
    """从任务记录+器官结果串提取本次度量(成功/步数/是否超时卡死), 供判进步对比。"""
    t = {}
    if task_id:
        try:
            t = c.task_get(task_id) or {}
        except Exception:
            t = {}
    res = str(result or "")
    failed = t.get("status") in ("failed", "cancelled") or "掉线" in res or "卡死" in res
    return {"ok": not failed, "steps": len(t.get("steps") or []),
            "over": ("卡死" in res or "掉线" in res), "at": time.time(), "note": res[:120]}

def record_result(c, task_type, organ, metrics, skill=None):
    """大loop干完同类任务: 对比本次vs上次(progress/<task_type>账). 进步才把改进版存回公共区+
    记progress账; 退步不存(用老版). 返回{verdict, stored, prev, cur}。"""
    key = f"progress/{task_type or organ}"
    prev = c.kv_get(key)
    verdict = judge_progress(prev, metrics)
    stored = False
    if verdict == "improve" and skill:
        from immune import skills as _sk
        base = c.kv_get(f"skills/general/{skill}") or c.kv_get(f"skills/specialized/{skill}") or {}
        body = dict(base) if isinstance(base, dict) else {"name": skill}
        body["practice"] = ((body.get("practice") or []) + [{
            "at": time.time(), "task_type": task_type, "steps": metrics.get("steps"),
            "note": metrics.get("note", "")}])[-4:]
        _sk.improve_and_store(c, "big_loop", skill, body,
                              note=f"{task_type}进步: 上次{prev.get('steps')}步→本次{metrics.get('steps')}步")
        c.emit("progress_stored", {"task_type": task_type, "organ": organ, "skill": skill,
                                   "verdict": verdict}, decay=0.2, source="core")
        stored = True
    c.kv_set(key, metrics)
    return {"verdict": verdict, "stored": stored, "prev": prev, "cur": metrics}

def _run_stage(c, organ, demand, task_id=None):
    """编排/派发的执行体: 复用现有器官入口(不新造协议)."""
    if organ == "coder":
        from organs import coder
        return coder.run(demand, c, task_id=task_id)
    if organ == "canvas":
        from organs import canvas_organ
        return canvas_organ.create(demand, c)
    if organ == "reflection":
        from organs import reflection
        return reflection.self_check(c, origin_text=demand)
    if organ == "dialogue":
        from organs import dialogue
        return dialogue.handle(demand, c)
    if organ == "wellness":
        from organs import wellness
        return wellness.run_one(c)
    return ""

def orchestrate(c, task_type, demand, task_id=None, order=None, run_stage=None):
    """大loop编排(静态模板第一步): task_type→建议顺序(order可override=强脑子路径), 查manifest找能力,
    逐个造临时loop→建任务→CAS claim→emit task_dispatch→执行→拆loop→判进步. 返回[{organ,result,verdict}...]。"""
    run_stage = run_stage or _run_stage
    stages = order or plan_stages(c, task_type)
    results = []
    for i, st in enumerate(stages):
        organ, cap = (st["organ"], st.get("cap")) if isinstance(st, dict) else (st, None)
        sid = f"{task_id or 'orch' + str(time.time_ns() % 10 ** 9)}-{i + 1}"
        loop = assemble_temp_loop(c, organ, task_type=task_type)
        try:
            c.task_create(sid, intent=task_type, assigned_to=organ,
                          params={"demand": demand, "loop": loop["id"]})
            c.task_claim(sid, owner_pid=os.getpid())          # 现有CAS claim
        except Exception:
            pass
        c.emit("task_dispatch", {"route_to": organ, "target": organ, "loop": loop["id"],
                                 "task_id": sid, "stage": i + 1, "cap": cap,
                                 "params": {"demand": demand}}, decay=0.1, source="core")
        try:
            res = run_stage(c, organ, demand, sid)
        except Exception as e:
            res = f"(执行异常: {str(e)[:120]})"
        teardown_temp_loop(c, loop["id"])
        pv = record_result(c, task_type, organ, _metrics_from_task(c, sid, res), skill=None)
        results.append({"organ": organ, "cap": cap, "loop": loop["id"],
                        "result": str(res), "verdict": pv["verdict"]})
    return results

def capability_route(c, sig_type: str, payload: str) -> dict:
    """master_message入口(2026-09-29统一自我改): 大脑不再亲自读主人的话替器官判断.
    主人的话默认直接透传给对话器官(dialogue), 只有命中'长任务/多器官协作'特征的才交大loop编排.
    2026-09-30主人定案(判断力第①层): 掂量≠认输——没现成能力不组织停'我不会', 交coder想办法达成.
    organ=None(匹配层判'干不了')时不再回dialogue, 改交coder(由NO_MATCH分支派活, 真解决).
    返回 {"organ": ..., "params": ..., "fallback": bool, "_capability": {...}|None}."""
    try:
        text = _extract_text(payload)
        if not text.strip():
            return {"organ": None, "params": {}, "fallback": True, "_capability": None}
        # 2026-10-02"让她自己造自己": 她脑子(dialogue)已掂出"要办事"并带结论递回→
        # 直接派coder执行, 不重新掂指令(判断已在她脑子做完, core只做执行管道)
        _pl = json.loads(payload) if isinstance(payload, str) else payload
        _pld = _pl.get("data", _pl) if isinstance(_pl, dict) else {}
        if isinstance(_pld, dict) and _pld.get("_self_intent"):
            return {"organ": "coder",
                    "params": {"demand": text, "_goal": _pld.get("_goal", ""), "_task_type": "code"},
                    "fallback": False, "_capability": _find_capability(c, "coder"),
                    "reason": "(她脑子掂出要办事, 自己递回core派执行)"}
        _tt = _detect_task_type(text)
        # 2026-09-30主人定案: 所有识别出的任务类型(modeling/mixed/canvas/code)都走LLM匹配,
        # 统一判"派给哪个器官". 原先只有modeling/mixed才匹配, code/canvas被透传dialogue=她"不会派活"的入口病.
        # 认不出(None)才透传dialogue(纯闲聊).
        if _tt in ("modeling", "mixed", "canvas", "code"):
            r = _llm_match(c, text)
            if r.get("organ"):
                if "/" in r["organ"]:
                    r["organ"] = r["organ"].split("/")[0].strip()
                # organ不在注册表(幻觉) → 判None, 让NO_MATCH派coder
                # 用已知器官集合兜底(各器官CAPABILITIES的organ名, 跟插件走动态取)
                _all_corgans = _known_organs(c)
                if r["organ"] and r["organ"] not in _all_corgans:
                    return {"organ": None, "params": {"demand": text, "_task_type": _tt}, "fallback": False,
                            "_capability": None, "reason": f"organ={r['organ']}不在能力清单, 交coder想办法达成"}
                # 2026-10-02主人定案: 对抗式审查用在"派活"——掂"这个能力真够得了这活没",
                # 拿能力描述(manifest)对着任务掂一遍. 够→派; 不够(像nas_disk只查本机够不着NAS)→转coder自己写.
                if r["organ"] not in ("dialogue", "coder") and not _capability_fits(c, r["organ"], text):
                    return {"organ": "coder", "params": {"demand": text, "_task_type": _tt}, "fallback": False,
                            "_capability": _find_capability(c, "coder"),
                            "reason": f"(掂派活: {r['organ']}够不着这活, 转coder自己想办法)"}
                r["fallback"] = False
                r.setdefault("params", {})["demand"] = text
                r.setdefault("params", {})["_task_type"] = _tt
                r["_capability"] = _find_capability(c, r["organ"])
                return r
            # 编排类型但LLM判organ=None("没现成能力") → 不再回dialogue, 回organ=None让NO_MATCH派coder
            return {"organ": None, "params": {"demand": text, "_task_type": _tt}, "fallback": False,
                    "_capability": None, "reason": r.get("reason", "") or "匹配层判无现成能力"}
        # 其它一律透传dialogue(统一自我), 大脑不替她判断
        return {"organ": "dialogue", "params": {"demand": text}, "fallback": False,
                "_capability": _find_capability(c, "dialogue")}
    except Exception as e:
        print(f"[意识核] 能力路由异常(统一自我→透传dialogue): {e}")
        return {"organ": "dialogue", "params": {}, "fallback": True, "_capability": None}

def _known_organs(c):
    """2026-09-30主人定案: 掂边界用已知器官集合(跟插件走动态取, 新增插件自动在列).
    从各器官CAPABILITIES + capabilities kv(按organ分key存) 收集organ名."""
    import importlib
    names = set()
    # 各器官模块的CAPABILITIES(跟插件走, 新增插件自动在列)
    for mod in ("dialogue", "reflection", "canvas_organ", "coder", "wellness"):
        try:
            m = importlib.import_module(f"organs.{mod}")
            for cap in getattr(m, "CAPABILITIES", []):
                if cap.get("organ"):
                    names.add(cap["organ"].strip())
        except Exception:
            pass
    # 从capabilities kv补(插件注册进State的)
    try:
        from immune import capabilities as _caps
        for cap in _caps.list_all(c):
            if cap.get("organ"):
                names.add(cap["organ"].strip())
    except Exception:
        pass
    return names

def _find_capability(c, organ: str):
    """掂边界用: 找器官对应的能力声明(含enabled/health), 喂给core做健康检查."""
    try:
        caps = c.kv_get(f"capabilities/{organ}")
        if isinstance(caps, list):
            enabled = [x for x in caps if x.get("enabled", True)]
            return enabled[0] if enabled else None
        return None
    except Exception:
        return None

def _capability_fits(c, organ: str, task_text: str) -> bool:
    """2026-10-02主人定案(对抗式审查用在派活): 掂"这个能力真够得了这活没".
    拿能力的manifest描述对着任务掂一遍——像nas_disk(只查本机df)够不着"去NAS查目录",
    这种就该转coder自己写(SSH登NAS), 不拿"名字像"就乱派.
    ponytail: 用规则掂主要矛盾(能力描述里写了"只能/本机/不含X"等限定词且任务要它做不到的事),
    拿不准默认够(True, 不误伤). 返回False=这能力够不着, 该转coder."""
    try:
        cap = _find_capability(c, organ)
        if not cap:
            return True  # 查不到能力描述→不瞎判, 默认够(不误伤正常派活)
        desc = (cap.get("description") or "") + " " + (cap.get("name") or "")
        desc_low = desc.lower()
        t = task_text.lower()
        # 主要矛盾掂法: 任务要"远程/别的设备/NAS/ssh"这类, 而能力描述明说是"本机/本地/定时/监测"
        # (nas_disk就是这种: 描述"定时df -h采集本机挂载点", 够不着"去NAS查目录")
        wants_remote = any(w in t for w in ("nas", "ssh", "远程", "别的机器", "别的设备", "那台", "家里"))
        is_local_only = any(w in desc_low for w in ("本机", "本地", "local", "定时", "scheduled", "监测", "采集"))
        if wants_remote and is_local_only:
            return False
        return True
    except Exception:
        return True


def main(once=False, interval=0.5):
    once = once or "--once" in sys.argv
    c = StateClient()
    print("[意识核v2] 心跳开始 · State服务已连接 · 等待信号...")
    # Phase1(Q3): 内置能力注册(幂等, 每次启动校验刷新)
    from immune import capabilities as _caps
    # 票1: 能力跟插件(manifest)走——各器官的CAPABILITIES从其 plugins/<organ>/manifest.yaml 读,
    # 这里是唯一注册点(替代原先dialogue/reflection内联在这里、其它器官各自散写的方式)
    from organs.dialogue import CAPABILITIES as _DIALOGUE_CAPS
    from organs.reflection import CAPABILITIES as _REFLECTION_CAPS
    from organs.canvas_organ import CAPABILITIES as _CANVAS_CAPS
    from organs.coder import CAPABILITIES as _CODER_CAPS
    from organs.wellness import CAPABILITIES as _WELLNESS_CAPS
    BUILTIN_CAPS = (_DIALOGUE_CAPS + _REFLECTION_CAPS + _CANVAS_CAPS
                    + _CODER_CAPS + _WELLNESS_CAPS)
    _reg_ok = 0
    for cap in BUILTIN_CAPS:
        try:
            ok, err = _caps.register(c, cap)
            _reg_ok += 1 if ok else 0
            if not ok:
                print(f"[意识核] 能力注册被拒: {cap['name']} — {err}")
        except Exception as e:
            print(f"[意识核] 能力注册异常: {cap['name']} — {e}")
    print(f"[意识核] 能力注册表就绪({_reg_ok}/{len(BUILTIN_CAPS)}内置能力)")
    # 票2: skill分级就绪(大loop派活现场grant/copy要用)
    from immune import skills as _skills
    _ng, _ns = _skills.load_skills(c)
    print(f"[意识核] skill分级就绪(general {_ng} / specialized {_ns})")
    # 地基②: core memory默认blocks(幂等seed)
    from immune import core_memory as _coremem
    _coremem.seed_defaults(c)
    print("[意识核] core memory就绪(persona/master/facts)")
    last_gc = time.time()
    last_active = time.time()
    try:
        while True:
            try:
                sigs = c.attention(focus_task=(c.kv_get("focus") or {}).get("task_id"))
                if not sigs:
                    last_active, triggered = maybe_idle_reflect(c, last_active)
                    if triggered:
                        print(f"[意识核] 空闲{IDLE_REFLECT_SECONDS:.0f}s → 发起轻反思")
                    # BDI重考虑: 空闲时重拾suspended任务(承诺了的事不能因为忙就丢)
                    _resumed = 0
                    for t in (c.tasks_by_status("suspended") or [])[:2]:
                        try:
                            c.task_requeue(t["task_id"], t.get("attempt", 1))
                            c.emit("task_dispatch", {"target": t.get("assigned_to", "dialogue"),
                                                     "params": t.get("params", {}),
                                                     "task_id": t["task_id"],
                                                     "resumed": True}, source="core")
                            _resumed += 1
                        except Exception:
                            pass
                    if _resumed:
                        print(f"[意识核] BDI重考虑: 重拾{_resumed}个suspended任务")
                        time.sleep(interval)
                        continue
                    time.sleep(interval)
                    continue
                top = sigs[0]
                last_active = time.time()
                # Phase2(Q1): master_message走能力注册表现场匹配; 其它信号走旧路由
                cr = {"organ": None, "params": {}, "fallback": True}
                if top["sig_type"] == "master_message":
                    cr = capability_route(c, top["sig_type"], top["payload"])
                    # 2026-09-30主人定案: 掂量≠认输. 掂完"我现在没这能力"不是停在那,
                    # 是转交coder自己想办法达成(写工具/查方案/降级), 交付才算完成
                    if not cr.get("fallback") and cr.get("organ") and cr.get("organ") not in ("coder", "dialogue"):
                        _cap = cr.get("_capability") or {}
                        if not _cap.get("enabled", True) or _cap.get("health") in ("degraded", "unhealthy"):
                            cr["organ"] = "coder"
                            cr["reason"] = f"(掂量: 能力{_cap.get('name', cr.get('organ'))}当前不可用, 转coder想办法达成)"
                            cr.setdefault("params", {})["demand"] = _extract_text(top["payload"])
                to = None
                if not cr.get("fallback"):
                    to = cr["organ"]
                    if to is None:
                        # 2026-09-30主人定案: 掂量≠认输. 没现成能力=交coder自己想办法达成,
                        # 不停在"我不会"也不瞎答应. coder造工具/查方案/降级, 真做成才交付
                        _pl = json.loads(top["payload"]) if isinstance(top["payload"], str) else top["payload"]
                        _d = _pl.get("data", _pl) if isinstance(_pl, dict) else {}
                        _txt = str(_d.get("text", ""))[:200] if isinstance(_d, dict) else ""
                        c.kv_append("capabilities/learn_queue",
                                    {"at": time.time(), "text": _txt, "reason": cr.get("reason", ""),
                                     "note": "转coder想办法达成, 非待学认输"})
                        # 2026-09-30审查: learn_queue无上限会无限增长, 保留最近200条
                        _lq = c.kv_get("capabilities/learn_queue") or []
                        if len(_lq) > 200:
                            c.kv_set("capabilities/learn_queue", _lq[-200:])
                        from organs import coder as _coder
                        _loop = assemble_temp_loop(c, "coder", task_type="code")
                        tid = f"t{int(time.time())}"
                        c.task_create(tid, intent="code_task", assigned_to="coder",
                                      params={"demand": _txt, "loop": _loop["id"]})
                        c.emit("task_dispatch", {"route_to": "coder", "target": "coder",
                                                  "loop": _loop["id"], "params": {"demand": _txt},
                                                  "task_id": tid}, source="core")
                        print(f"[意识核] NO_MATCH({cr.get('reason')}) → 交coder想办法达成(造工具/查方案/降级)")
                        c.drop(top["sig_id"])
                        continue
                if not to:  # 匹配层回退/非master_message: 旧路由
                    to = route_to(top["payload"])
                    if not to:
                        rule = ROUTE_TABLE.get(top["sig_type"],
                                               {"priority": 3, "decay": 0.3, "to": "suspended"})
                        to = rule["to"]
                print(f"[意识核] 拾取 {top['sig_type']} (强度{top['strength']:.2f}) → {to}" +
                      (f" (匹配: {cr.get('reason','')})" if not cr.get("fallback") and cr.get("reason") else ""))
                try:
                    # per-signal隔离: 单个器官异常不杀意识核(转loop_error回注)
                    # 票2: 长问题(跨多器官) → 大loop编排, 按manifest建议顺序起临时loop跑
                    # 统一自我(9/29): 编排类型(modeling/mixed)才走大loop, 其它全透传dialogue一个人格回
                    _req = cr.get("params") or {}
                    _orch_tt = _req.get("_task_type") or (_detect_task_type(
                        _req.get("demand") or _extract_text(top["payload"])) if top["sig_type"] == "master_message" else None)
                    if (top["sig_type"] == "master_message" and to == "coder"
                            and not _req.get("no_orchestrate")
                            and _orch_tt in ("modeling", "mixed")):
                        _demand = _req.get("demand") or _extract_text(top["payload"])
                        _st = orchestrate(c, _orch_tt, _demand)
                        reply = ("编排完成[" + "→".join(x["organ"] for x in _st) + "] "
                                 + " | ".join(f"{x['organ']}: {x['result'][:200]}" for x in _st))[:1500]
                        print(f"[大loop编排] {reply[:120]}")
                        _pl = top.get("payload")
                        if isinstance(_pl, str):
                            try: _pl = json.loads(_pl)
                            except Exception: _pl = {}
                        if isinstance(_pl, dict) and (_pl.get("data", {}) or {}).get("from_wechat"):
                            c.kv_append("wechat_outbox", {"to": "owner", "text": reply, "from_organ": "dialogue"})
                        c.drop(top["sig_id"])
                        continue
                    if to == "dialogue":
                        # 身份权限(2026-09-15主人提议): sender!=owner → 客人模式
                        _pl_d = top.get("payload")
                        if isinstance(_pl_d, str):
                            try: _pl_d = json.loads(_pl_d)
                            except Exception: _pl_d = {}
                        if isinstance(_pl_d, dict):
                            _sender = (_pl_d.get("data", {}) or {}).get("sender", "")
                            _owner = c.kv_get("wechat/owner") or ""
                            _pl_d["data"]["is_master"] = (not _owner) or (_sender == _owner)
                            top["payload"] = json.dumps(_pl_d, ensure_ascii=False)
                        # 9/29: 微信聊天频道交给【消息小loop】直接回话(不绕core)
                        # 其它来源的信号, core仍亲自调dialogue
                        _pl_chk = top.get("payload")
                        if isinstance(_pl_chk, str):
                            try: _pl_chk = json.loads(_pl_chk)
                            except Exception: _pl_chk = {}
                        _from_wechat = bool((_pl_chk.get("data", {}) or {}).get("from_wechat"))
                        if _from_wechat:
                            from plugins.message_loop import main as _mloop
                            _mloop.on_event({"sig_type": "master_message",
                                             "payload": top.get("payload")})
                            print(f"[消息小loop] 微信聊天已交message_loop回话")
                        else:
                            reply = dialogue.handle(top["payload"], c)
                            print(f"[诺诺] {reply}")
                            c.kv_append("wechat_outbox", {"to": "owner", "text": reply,
                                                          "from_organ": "dialogue"})
                    elif to == "master_alert":
                        print(f"[!!] 需要打扰主人: {top['payload']}")
                    elif to == "coder":
                        # Phase5: 编程器官——真实任务执行+自进化
                        from organs import coder
                        _demand = cr.get("params", {}).get("demand") or ""
                        _tt = _detect_task_type(_demand) or "code"
                        _loop = assemble_temp_loop(c, "coder", task_type=_tt)   # 票2: 现拼临时loop
                        tid = f"t{int(time.time())}"
                        c.task_create(tid, intent="code_task", assigned_to="coder",
                                      params={"demand": _demand, "loop": _loop["id"]})
                        # ScalingInter: 匹配时的难度判定顺带传给coder(匹配理由里含难度词就复用, 否则让匹配LLM补判)
                        _dm = (cr.get("params", {}).get("difficulty")
                               or _guess_difficulty(cr.get("reason", ""), demand=_demand or str(cr.get("params", {}))))
                        try:
                            reply = coder.run(_demand, c, task_id=tid, difficulty=_dm)
                        finally:
                            teardown_temp_loop(c, _loop["id"])                  # 干完活删loop
                        _pv = record_result(c, _tt, "coder",                        # 判进步存经验
                                            _metrics_from_task(c, tid, reply), skill="coding-python")
                        print(f"[coder] {reply[:100]} (判进步:{_pv['verdict']})")
                        _pl = top.get("payload")
                        if isinstance(_pl, str):
                            try: _pl = json.loads(_pl)
                            except Exception: _pl = {}
                        # 回执路由(2026-09-15主人反馈: 诺诺派的活进度别刷屏主人):
                        # from_wechat=True(主人微信亲手发的) → 回执发主人
                        # 其他来源(诺诺派/系统自派) → 回执写State任务区(派活方读取), 不打扰主人
                        if isinstance(_pl, dict) and (_pl.get("data", {}) or {}).get("from_wechat"):
                            c.kv_append("wechat_outbox", {"to": "owner", "text": reply[:1500], "from_organ": "dialogue"})
                        else:
                            _src = ((_pl.get("data", {}) or {}).get("from", "") or "system") if isinstance(_pl, dict) else "system"
                            c.kv_append(f"task_receipt/{tid}", {
                                "to": _src, "demand": (cr.get("params", {}).get("demand") or "")[:100],
                                "reply": reply[:1500], "at": __import__("time").time()})
                            # 重大成果也走主动分享通道(4h限频, reflection那套)
                            if "完成" in reply[:20] or "搞定" in reply[:20]:
                                try:
                                    from organs.reflection import _humanize_push
                                    _humanize_push(c, reply)
                                except Exception:
                                    pass
                    elif to == "canvas":
                        # Phase3: 画布器官(从dialogue迁出)——params.demand来自大loop匹配
                        from organs import canvas_organ
                        _loop = assemble_temp_loop(c, "canvas", task_type="canvas")   # 票2: 现拼临时loop
                        try:
                            reply = canvas_organ.create(cr.get("params", {}).get("demand") or "", c)
                        finally:
                            teardown_temp_loop(c, _loop["id"])
                        record_result(c, "canvas", "canvas", _metrics_from_task(c, None, reply))
                        print(f"[canvas] {reply}")
                        _pl = top.get("payload")
                        if isinstance(_pl, str):
                            try: _pl = json.loads(_pl)
                            except Exception: _pl = {}
                        if isinstance(_pl, dict) and (_pl.get("data", {}) or {}).get("from_wechat"):
                            c.kv_append("wechat_outbox", {"to": "owner", "text": reply, "from_organ": "dialogue"})
                    elif to == "reflection":
                        reflection.handle(top["sig_type"], top["payload"], c)
                        # 问题③修复(2026-09-13): reflection吸收master_message后原路回执——
                        # 微信主人必须收到回音, 否则黑洞( previously静默吞掉)
                        _pl = top.get("payload")
                        if isinstance(_pl, str):
                            try: _pl = json.loads(_pl)
                            except Exception: _pl = {}
                        if isinstance(_pl, dict) and (_pl.get("data", {}) or {}).get("from_wechat"):
                            _ack = reflection.self_check(c, origin_text="master_message吸收")
                            c.kv_append("wechat_outbox", {"to": "owner", "text": _ack[:1200], "from_organ": "dialogue"})
                    elif to == "wellness":
                        # wellness器官: 主人点名体检 → 跑一次完整体检+白名单自愈, 回一句人话
                        from organs import wellness
                        _rep = wellness.run_one(c)
                        reply = (f"体检完了: 脑子{'通' if _rep['brain'] else '兜底'}, "
                                 f"器官挂{len(_rep['organs_dead'])}个, "
                                 f"清毒信号{_rep['poison_cleared']}条, "
                                 f"日志暗病{len(_rep['log_errors'])}条")
                        print(f"[wellness] {reply}")
                        _pl = top.get("payload")
                        if isinstance(_pl, str):
                            try: _pl = json.loads(_pl)
                            except Exception: _pl = {}
                        if isinstance(_pl, dict) and (_pl.get("data", {}) or {}).get("from_wechat"):
                            c.kv_append("wechat_outbox", {"to": "owner", "text": reply, "from_organ": "dialogue"})
                    else:
                        print(f"[意识核] → {to}")
                except Exception as e:
                    print(f"[意识核] 器官处理异常: {e} → 转loop_error")
                    c.emit("loop_error", {"orig": top["sig_type"], "err": str(e)[:200],
                                          "payload": top["payload"]},
                           decay=0.1, source="core")
                c.drop(top["sig_id"])
            except Exception as e:
                # P0修复(2026-09-19): 毒信号防死循环 -- 同一信号连续失败3次强制drop
                # (曾因双重编码payload+P0信号永不衰减, core死循环3188次/24h)
                _fid = f"_poison_{top['sig_id']}"
                _cnt = (POISON_CNT.get(_fid, 0) + 1)
                POISON_CNT[_fid] = _cnt
                print(f"[意识核] 循环级异常(不退出): {e}")
                if _cnt >= 3:
                    print(f"[意识核] 毒信号{top['sig_id']}连续失败{_cnt}次, 强制drop防死循环")
                    POISON_CNT.pop(_fid, None)
                    try: c.drop(top["sig_id"])
                    except Exception: pass
                time.sleep(2)
            try:
                c.checkpoint()
            except Exception:
                pass
            # 每小时GC: 清理空kv(防膨胀)
            if time.time() - last_gc > 3600:
                last_gc = time.time()
            if once:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[意识核] 收到停止信号, 拍快照退出")
    finally:
        try: c.checkpoint()
        except: pass
        c.close()

if __name__ == "__main__":
    main()
