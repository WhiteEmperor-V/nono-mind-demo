#!/usr/bin/env python3
"""编程loop器官(coder): 新身体的手——写代码/执行/调试/自进化
Phase5: 收task_dispatch{target:coder, params:{demand}} →
  计划拆步 → 每步[LLM生成代码→子进程执行→Observation] → 失败: 修/上网查/放弃 → 结果回报
自进化: 查到的解法写认知库(State kv: coder/knowledge/{topic}), 下次同类问题先查知识库
安全: 子进程timeout 60s/输出截断4KB/禁root/查文档走独立web_search(不进子进程)
"""
import json, time, os, subprocess, sys, re

MAX_STEPS = 8
SUB_TIMEOUT = 60
OUT_LIMIT = 4096
WORKDIR = "/root/nono-mind/coder_workspace"
KV_KNOWLEDGE = "coder/knowledge/"

from immune.plugins import capabilities_from_manifest, track_progress

# 票1: 能力跟插件走——从 plugins/coder/manifest.yaml 读(不再散写在此)
CAPABILITIES = capabilities_from_manifest("coder")

def _llm(messages, max_tokens=1500, timeout=120):
    from llm_client import chat
    return chat(messages, max_tokens=max_tokens, timeout=timeout)

def _knowledge_lookup(topic: str, c) -> str:
    """认知库查询: 相关主题的历史解法"""
    store = c.kv_list(KV_KNOWLEDGE)
    topic_l = topic.lower()[:30]
    hits = []
    for k, v in store.items():
        if topic_l in k.lower() or any(w in k.lower() for w in topic_l.split()):
            hits.append(f"[{k}] {str(v)[:300]}")
    return "\n".join(hits[:3]) if hits else "(知识库无相关记录)"

def _code_version() -> str:
    """当前代码版本(git短哈希)——dg-piagent版本协议: 知识必须带验证时的版本戳"""
    try:
        r = subprocess.run(["git", "-C", "/root/nono-mind", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _humanize(result: str, ok: bool, demand: str = "") -> str:
    """把coder的机器播报转成主人听得懂的人话(只给一句明确结论, 不拼中间机器词).
    主人9/15反馈"播报太机器"; 10/02反馈"乱说话"——根因是开头结论(搞定/没干完)和
    后面拼的机器result(已完成:/未完成:)两套话打架. 这里统一: 把result里所有机器
    结论词清掉, 只留结果内容, 开头只报一句结论."""
    r = (result or "").strip()
    r = re.sub(r"\(\d+步预算\)", "", r)
    # 剥掉机器前缀"任务完成(预算复核, 已校验: ...)"里的内部状态(预算复核/已校验), 但保留校验后的事实句
    r = re.sub(r"任务完成\(\s*预算复核,?\s*已校验[:：]\s*", "", r)
    r = re.sub(r"任务完成\(\s*已校验[:：]\s*", "", r)
    r = re.sub(r"任务完成\(\s*", "", r)
    r = re.sub(r"已校验[:：]", "", r)
    r = re.sub(r"任务(完成|未完成)[:：]\s*", "", r)
    r = re.sub(r"预算复核。?", "", r)
    r = re.sub(r"^未完成[:：]\s*", "", r)
    r = re.sub(r"\b(已完成|未完成)[:：]\s*", "", r).strip()
    r = re.sub(r"[（(]已校验[:：][^）)]*[）)]", "", r).strip()
    r = r.strip().rstrip(")").strip()   # 清掉剥内部状态后残留的多余右括号
    if len(r) > 180:
        r = r[:177] + "..."
    if ok:
        return f"搞定啦~ {r}" if r else "搞定啦[得意]"
    return f"这活还没干完... {r}" if r else "这活还没干完, 我回头接着弄[委屈]"

def _verify_task(c, demand: str, code: str, lang: str, task_id: str) -> tuple:
    """完成校验(2026-09-30主人纠正: LLM自报完成不可信, 必须真验结果).
    让一个独立的LLM调用看任务+上一步代码, 判断结果是否真达成.
    返回(ok: bool, msg: str). 校验调用失败时保守判False(宁报'没做成', 不冒充完成)."""
    try:
        vmsg = _llm([
            {"role": "system", "content": (
                "你是结果校验员。给你任务需求和最后一步代码/输出, 只判断'任务结果是否真正达成'。"
                "代码跑通/无报错≠完成, 要看需求里的目标是否真达成。"
                "只回答一行: '已达成: <证据>' 或 '未达成: <缺什么>'。禁止臆测已完成。")},
            {"role": "user", "content": (
                f"任务需求: {demand}\n\n"
                f"最后一步代码(lang={lang}):\n{code[:1500]}\n\n"
                f"判断结果是否真正达成任务需求?")},
        ], max_tokens=500, timeout=120)
        vmsg = (vmsg or "").strip()
        if vmsg.startswith("已达成"):
            return True, vmsg.split(":", 1)[-1].strip()[:60] or "已达成"
        # 未达成 / 模糊 / 空 → 保守判没做到
        return False, (vmsg[:60] if vmsg else "校验超时, 未确认达成")
    except Exception as e:
        return False, f"校验失败({str(e)[:40]}), 未确认达成"

def _adversarial_review(c, demand: str, code: str, lang: str, task_id: str) -> str:
    """对抗式审查(2026-09-30主人定案, 视频'神级prompt'第②招): 交付前强制切'找茬身份'.
    拿自己的产出(代码/结果)对抗挑毛病——边界条件/空输入/异常/漏掉的步骤.
    ponytail: 只出简短找茬清单, 喂回主循环继续修; 挑不出漏洞才算站得住.
    返回找茬文本(空=没挑出毛病, 产出站得住)."""
    try:
        crit = _llm([
            {"role": "system", "content": (
                "你是专门找茬的测试员(对抗式审查). 给你一段代码和它要达成的任务, "
                "专挑毛病: 边界条件没处理/空输入会崩/异常没兜/漏了任务要求/有隐蔽bug. "
                "挑不出真毛病就答'无', 有就列1-3条(每条一行). 只输出找茬清单, 不输出别的.")},
            {"role": "user", "content": (
                f"任务: {demand}\n\n代码(lang={lang}):\n{code[:2000]}\n\n"
                f"对抗审查: 挑这段代码在这个任务下的毛病(挑不出答'无').")},
        ], max_tokens=400, timeout=90)
        crit = (crit or "").strip()
        # 没挑出/没回 → 产出站得住
        if not crit or crit in ("无", "没有", "无问题", "挑不出") or "无" == crit[:1]:
            return ""
        return f"[对抗审查找茬] {crit[:400]}"
    except Exception:
        return ""  # 审查本身失败不阻塞交付(保守: 不卡主流程)

def _simple_task(text: str) -> bool:
    """2026-09-30判断力第②层(掂档位): 掂"轻活"——统计/列出/查文件/简单脚本, 别拉满.
    ponytail: 不加LLM调用, 关键词够(轻活特征明显). 返回True=该提前收工.
    2026-09-30审查修: '看看'太宽(可能重活), 加负向词'代码/逻辑/架构/bug/重构'兜住."""
    t = text.lower()
    # 负向词命中 → 不算轻活(别把"看看代码"判成轻活)
    if any(w in t for w in ("代码", "逻辑", "架构", "bug", "重构", "设计", "复杂", "多步", "调试", "爬", "调研", "对比", "系统", "核心", "模块")):
        return False
    if any(w in t for w in ("统计", "列出", "多少", "个数", "数量", "查一下", "读一下", "简单")):
        return True
    return False

def _maybe_tool_factory(code: str, lang: str, demand: str, task_id: str, c) -> str:
    try:
        # 候选代码: 传入的code太短时, 从workspace找任务期间的产物(含def的py, 排除step_验证脚本)
        cand = code if (lang == "python" and len(code) >= 400 and ("def " in code or "class " in code)) else ""
        if not cand:
            import glob as _glob
            cands = []
            for p in _glob.glob(os.path.join(WORKDIR, "*.py")):
                try:
                    if os.path.getmtime(p) < time.time() - 3600:
                        continue
                    t = open(p).read()
                    if ("def " in t or "class " in t) and len(t) >= 300 and "step_" not in os.path.basename(p):
                        cands.append((os.path.getmtime(p), p, t))
                except Exception:
                    pass
            if cands:
                cand = sorted(cands)[-1][2]
        if not cand or ("def " not in cand and "class " not in cand):
            return ""
        code = cand
        lang = "python"
        # 危险模式审查(硬门控)
        dangerous = re.search(r"rm\s+-rf\s+/|shutdown|mkfs|dd\s+if=/dev/", code)
        if dangerous:
            return ""
        name = re.sub(r"[^a-z0-9]+", "_", demand.lower())[:24].strip("_") or f"tool_{task_id}"
        tools_dir = os.path.join(WORKDIR, "tools")
        os.makedirs(tools_dir, exist_ok=True)
        path = os.path.join(tools_dir, f"{name}.py")
        wrapper = (
            f'#!/usr/bin/env python3\n'
            f'"""自造工具: {demand[:80]}\n来源: task/{task_id} | 生成: {time.strftime("%F %T")}\n'
            f'用法: python3 {name}.py "<参数/需求文本>"\n"""\n'
            f'import sys\n\n'
            f'ORIG_DEMAND = {demand[:100]!r}\n\n'
            + code +
            f'\n\nif __name__ == "__main__":\n'
            f'    _arg = " ".join(sys.argv[1:]) or ORIG_DEMAND\n'
            f'    print(f"工具{name}被调用(参数: {{_arg[:50]}})。此工具为自动封装, 复用逻辑见源码。")\n'
        )
        with open(path, "w") as fh:
            fh.write(wrapper)
        # 语法门控
        import py_compile as _pc
        _pc.compile(path, doraise=True)
        # 动态注册: 现场重扫manifest把新插件/新能力注册进能力表(不靠重启core, 造完即用)
        try:
            from immune.plugins import rescan_and_register
            new_caps, _ = rescan_and_register(c)
            if new_caps:
                print(f"[工具自造] 动态注册: 新增{new_caps}条能力(不重启core)")
        except Exception as _re:
            print(f"[工具自造] 动态注册跳过(不影响造插件本身): {_re}")
        # 注册进能力注册表(对话匹配层可命中; description带边界)
        c.kv_set(f"capabilities/tools/{name}", {
            "organ": "coder", "name": name,
            "description": f"自造工具: {demand[:60]}。适合: 与\u0022{demand[:30]}\u0022同类需求。不适合: 无关任务",
            "script": path, "at": time.time(), "from_task": task_id})
        # 插件形态(万物皆可插件): 同步生成dsh TS插件+cordis.patch.yml → 诺诺hermes可调
        try:
            plug_dir = f"/root/dsh-home/my-plugins/{name}"
            os.makedirs(plug_dir, exist_ok=True)
            ts = f"""/**
 * 自造工具插件: {demand[:60]}
 * 来源: task/{task_id} | 生成: {time.strftime('%F %T')} | by nono-mind工具自造
 * @module {name}
 */
import {{ execFile }} from 'node:child_process'
import type {{ Context }} from '@deepseek-ai/cordis'
import {{ defineTool }} from '@deepseek-ai/dsh-tools'

export const name = '{name}'
export const inject = ['tools']

export function apply(ctx: Context) {{
  ctx.tools.register(defineTool({{
    name: '{name}',
    description: {json.dumps(demand[:120], ensure_ascii=False)},
    parameters: {{}},
    output: {{ schema: {{ type: 'string' }}, render: (_a, v) => [{{ type: 'text', text: String(v) }}] }},
    async execute(args, exec) {{
      const arg = (args && args.input) ? String(args.input) : ''
      return await new Promise((resolve, reject) => {{
        execFile('python3', [{path!r}, arg], {{ signal: exec.signal, timeout: 60000 }},
          (err, stdout, stderr) => {{
            if (err) reject(new Error(stderr.trim() || err.message))
            else resolve(stdout.trim())
          }})
      }})
    }},
  }}))
}}
"""
            with open(os.path.join(plug_dir, f"{name}.ts"), "w") as tf:
                tf.write(ts)
            patch = f"""- insert:
    - id: {name}
      name: '{plug_dir}/{name}.ts'
"""
            with open(os.path.join(plug_dir, "cordis.patch.yml"), "w") as pf:
                pf.write(patch)
            print(f"[工具自造] dsh插件已生成: {plug_dir}/{name}.ts (nono_dsh.py --patch {plug_dir}/cordis.patch.yml 可挂载)")
        except Exception as pe:
            print(f"[工具自造] dsh插件生成失败(不影响nono-mind侧): {pe}")
        print(f"[工具自造] 注册新能力: {name} → {path}")
        return f"（已把这段代码封装成新工具{name}, 下次同类活直接用）"
    except Exception as e:
        print(f"[工具自造] 跳过: {e}")
        return ""


def _knowledge_save(topic: str, solution: str, c):
    """自进化: 解法入库(dg-piagent式: 场景化key+版本戳)"""
    key = KV_KNOWLEDGE + topic.lower().replace(" ", "_")[:40]
    c.kv_set(key, {"at": time.time(), "solution": solution[:800],
                   "code_version": _code_version()})

def _run_code(code: str, lang: str = "python") -> dict:
    """子进程执行. 返回{ok, stdout, stderr, returncode, out_h, err_h}. 安全: timeout/截断/独立进程
    SoL-Pi ObservationPack: 长输出自动归档, obs_summary只带句柄+摘要, LLM要细节时fetch_handle按页取回"""
    os.makedirs(WORKDIR, exist_ok=True)
    ext = "py" if lang == "python" else "sh"
    path = os.path.join(WORKDIR, f"step_{int(time.time()*100)}.{ext}")
    with open(path, "w") as f:
        f.write(code)
    runner = [sys.executable, path] if lang == "python" else ["bash", path]
    try:
        r = subprocess.run(runner, capture_output=True, text=True, timeout=SUB_TIMEOUT,
                           cwd=WORKDIR)
        raw_out, raw_err = r.stdout[:OUT_LIMIT], r.stderr[:OUT_LIMIT]
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"超时(>{SUB_TIMEOUT}s)", "returncode": -1,
                "path": path, "out_h": None, "err_h": None}
    from immune.obs_archive import archive_output
    o, e = archive_output(raw_out, "out"), archive_output(raw_err, "err")
    return {"ok": r.returncode == 0, "stdout": o["excerpt"], "stderr": e["excerpt"],
            "returncode": r.returncode, "path": path,
            "out_h": o["handle"], "err_h": e["handle"],
            "out_full_len": o["full_len"], "err_full_len": e["full_len"]}

def _web_search(query: str) -> str:
    """上网查方法(自进化入口). 走Hermes的web_search via hermes_tools"""
    try:
        sys.path.insert(0, "/root/nono-mind")
        from hermes_tools import web_search
        res = web_search(query, limit=3)
        items = (res.get("data") or {}).get("web") or []
        return "\n".join(f"- {x.get('title','')[:60]}: {x.get('url','')}" for x in items[:3]) or "(无结果)"
    except Exception as e:
        return f"(搜索失败: {str(e)[:80]})"

def run(demand: str, c, task_id: str = None, difficulty: str = "medium") -> str:
    """编程任务主循环(OpenHands Action→Observation式)
    difficulty: 大loop匹配时判定的任务难度(simple/medium/hard)——决定步数预算系数"""
    if task_id:
        try: c.task_claim(task_id, owner_pid=os.getpid())
        except Exception: pass
    # ScalingInter: 步数预算按能力画像动态算(练得越熟→链条越长)
    from immune.evals import steps_budget
    budget = steps_budget(c, "coder", difficulty)
    print(f"[coder] 步数预算: {budget} (难度:{difficulty})")
    # 步骤0: 知识库先行
    know = _knowledge_lookup(demand, c)
    # 步骤0.5: 代码地图(Graphify AST思想)——涉及本仓库的任务先给LLM地图, 省去乱读文件
    map_digest = ""
    if any(w in demand for w in ("nono-mind", "本仓库", "器官", "意识核", "state", "loop.py", "coder", "dialogue")):
        try:
            from immune import codemap
            map_digest = codemap.to_digest(codemap.build_map("/root/nono-mind"), max_lines=30)
        except Exception:
            pass
    plan_msg = [{"role": "system", "content":
        "你是编程器官。给一个任务, 输出第一步要执行的代码(python或bash, 单文件, 自包含)。"
        "只输出代码块```lang\n...\n```和一行PLAN: 下一步计划(50字内)。\n"
        "【第一性原理(主人9/30定)】别照字面/惯例做——先掂'主人真正要的本质结果是啥', "
        "哪些是必须做的、哪些只是惯性做法; 安全能自己做完的直接做完, 别把活推回主人。"
        "只有真正危险/需主人拍板的才停下请示, 其余自己解决。"
        + (f"\n知识库相关经验:\n{know}" if know != "(知识库无相关记录)" else "")
        + (f"\n本仓库代码地图(函数级, 用它定位文件再写代码, 不要盲猜路径):\n{map_digest}" if map_digest else "")},
        {"role": "user", "content": demand}]
    steps_log = []
    last_ok_code, last_ok_lang = "", ""
    obs_summary = ""
    streak_ok = 0  # ScalingInter: 连续成功计数(预算续期用)
    from immune.stuck_detector import StuckDetector
    stuck = StuckDetector()
    stuck_nudge = ""  # 死循环纠偏指令(注入下一步prompt)
    steer_texts = []  # Pi式steering: 主人中途指示(注入下一步prompt)
    followups = []    # Pi式followup: 当前任务完成后追加的新任务
    for step in range(1, budget + 1):
        # Pi式steering轮询: 取消/中途指示/追加任务 三合一(信号来源=State, 器官不私通)
        if task_id:
            t = c.task_get(task_id) or {}
            if t.get("status") == "cancelling":
                c.task_finish(task_id, status="cancelled", result=f"主人在第{step}步叫停")
                return f"任务已取消(第{step}步)[ANGED]"
            try:
                from immune.steering import poll as _steer_poll
                si = _steer_poll(c, task_id)
                if si["steer"]:
                    steer_texts.extend(si["steer"])
                if si["followup"]:
                    followups.extend(si["followup"])
            except Exception:
                pass
        # LLM生成这一步的代码
        _steer_block = ""
        if steer_texts:
            from immune.steering import render_nudge
            _steer_block = "\n" + render_nudge(steer_texts[-3:])  # 最多带最近3条
        msgs = plan_msg + ([{"role": "user", "content": f"到目前为止的执行记录:\n{obs_summary}\n请输出下一步代码。"
                             + (f"\n{stuck_nudge}" if stuck_nudge else "")
                             + _steer_block}] if obs_summary else [])
        try:
            raw = _llm(msgs, timeout=300)  # 写整页代码需长超时(3D游戏HTML数千行)
        except Exception as e:
            c.task_finish(task_id, "failed", f"LLM失败: {e}") if task_id else None
            return f"编程大脑掉线了({str(e)[:60]}), 任务暂停[难过]"
        # 解析代码块
        code, lang, plan = "", "python", ""
        in_code = False
        for ln in raw.split("\n"):
            if ln.strip().startswith("```"):
                in_code = not in_code
                if in_code and len(ln.strip()) > 3:
                    lang = ln.strip()[3:] or "python"
                continue
            if in_code:
                code += ln + "\n"
            elif ln.upper().startswith("PLAN:"):
                plan = ln[5:].strip()
        # TASK_COMPLETE检测: LLM判定任务完成
        if "TASK_COMPLETE:" in raw:
            result = raw.split("TASK_COMPLETE:")[1].strip()[:600]
            if task_id: c.task_finish(task_id, "done", result)
            _knowledge_save(demand[:30], f"成功解法要点: {plan or result}", c)
            # 地基④: 成功过程沉淀为过程性记忆(Voyager技能库思想)——下次先复用
            # 信任标签: 脚本真跑通了=EXTRACTED实证级
            from immune import trust
            c.kv_set(f"procedures/{task_id or int(time.time())}", trust.stamp({
                "at": time.time(), "demand": demand[:200], "steps": steps_log, "code_version": _code_version(),
                "result": result[:400], "verified": True}, "EXTRACTED",
                evidence=f"task/{task_id} steps={len(steps_log)}"))
            # 地基⑥: 自评价
            from immune.evals import self_eval as _sev
            _sev(c, task_id, "coder", steps_log, demand)
            _tool_note = _maybe_tool_factory(last_ok_code or code, last_ok_lang or lang, demand, task_id or "", c)
            return _humanize(result + (" " + _tool_note if _tool_note else ""), True, demand)
        if not code.strip():
            # 没输出代码=可能任务已完成, 让LLM总结
            result = raw[:500]
            if task_id: c.task_finish(task_id, "done", result)
            _knowledge_save(demand[:30], f"任务完成. 最后回复: {result}", c) if result else None
            return _humanize(result, True, demand)
        # 执行(Action→Observation)
        obs = _run_code(code, lang)
        if obs["ok"]:
            last_ok_code, last_ok_lang = code, lang
        steps_log.append({"step": step, "lang": lang, "code_len": len(code),
                          "ok": obs["ok"], "stderr": obs["stderr"][:200]})
        if task_id:
            t = c.task_get(task_id) or {}
            if t:
                t["steps"] = steps_log
                c.kv_set(f"tasks/{task_id}", t)
        # SoL-Pi句柄化: 长输出只进摘要+句柄(细节LLM要时fetch_handle取回)
        _hs = f" [out句柄:{obs['out_h']}, 完整{obs.get('out_full_len', 0)}字]" if obs.get("out_h") else ""
        _he = f" [err句柄:{obs['err_h']}]" if obs.get("err_h") else ""
        obs_summary += (f"\n[step{step}] {'成功' if obs['ok'] else '失败'} "
                        f"stdout={obs['stdout'][:300]}{_hs} stderr={obs['stderr'][:300]}{_he}\n"
                        f"(要看输出细节, 在CODE里用: from immune.obs_archive import fetch_handle; print(fetch_handle('句柄', 页码)))")
        # ScalingInter动态续期: 连续2步全成功 → 预算+2(练着练着变强了), 上限10
        if obs["ok"]:
            streak_ok += 1
            if streak_ok >= 2 and budget < 10:
                budget += 2
                streak_ok = 0
                print(f"[coder] 连续成功, 预算续期→{budget}")
        else:
            streak_ok = 0
        # 2026-09-30判断力第②层(掂档位): 难度路由——轻活别拉满, 重活别打折.
        # 掂出简单活且已连成 → 提前收工, 省步数省token; 复杂继续.
        if obs["ok"] and step >= 1 and _simple_task(demand) and streak_ok >= 1:
            result = f"任务完成(掂档位: 简单活, {step}步够, 提前收工不拉满)。{obs['stdout'][:120]}"
            if task_id: c.task_finish(task_id, "done", result)
            from immune.evals import self_eval as _sev
            _sev(c, task_id, "coder", steps_log, demand)
            return _humanize(result, True, demand)
        # StuckDetector: 死循环检测(重复动作/重复报错/空观察/无进展)
        advice = stuck.observe(code, obs)
        if advice != "ok":
            stuck_nudge = stuck.nudge(advice)
            obs_summary += f"\n[stuck] {stuck_nudge}"
            if advice in ("repeat_error", "no_progress") and step >= budget - 2:
                # 接近预算上限还卡死 → 提前中止省token, 诚实报告; 卡死不计能力分
                result = f"任务卡死在第{step}步({advice}), 已中止。已做到: {obs_summary[-200:]}"
                if task_id: c.task_finish(task_id, "failed", result)
                from immune.evals import self_eval as _sev
                _sev(c, task_id, "coder", steps_log, demand, stuck_abort=True)
                return f"我卡住了({advice}), 任务中止[难过] 进度: {result[:100]}"
        else:
            stuck_nudge = ""
        # 服务端完成检测: 本步成功 + PLAN声明完成 → 结束(不依赖LLM输出特殊标记)
        # 2026-09-30主人纠正"其它任务也拉一匹": PLAN说完成=LLM自报不可信, 必须真校验结果.
        if obs["ok"] and step >= 2 and any(w in plan for w in ("完成", "已达成", "DONE", "done", "任务完成")):
            _v_ok, _v_msg = _verify_task(c, demand, code, lang, task_id)
            # 2026-09-30主案定案(视频第②招): 校验通过后强制对抗式审查——切找茬身份挑自己产出的毛病,
            # 挑出真毛病就喂回主循环继续修(不交付带病产出); 挑不出才站得住.
            _crit = _adversarial_review(c, demand, code, lang, task_id) if _v_ok else ""
            if _v_ok and _crit:
                obs_summary += f"\n{_crit}\n[对抗审查] 挑出毛病, 继续修到站得住再报完成."
                print(f"[coder] 对抗审查挑出毛病, 继续修: {_crit[:100]}")
                continue
            if _v_ok:
                result = f"任务在第{step}步完成(已校验: {_v_msg})。{plan}"
                if task_id: c.task_finish(task_id, "done", result)
                _knowledge_save(demand[:30], f"执行了{step}步完成(校验通过)。要点: {obs_summary[-300:]}", c)
                # 地基④: 成功过程沉淀为过程性记忆
                from immune import trust
                c.kv_set(f"procedures/{task_id or int(time.time())}", trust.stamp({
                    "at": time.time(), "demand": demand[:200], "steps": steps_log, "code_version": _code_version(),
                    "result": result[:400], "verified": True, "verify_msg": _v_msg}, "EXTRACTED",
                    evidence=f"task/{task_id} steps={len(steps_log)}"))
                # 地基⑥: 自评价
                from immune.evals import self_eval as _sev
                _sev(c, task_id, "coder", steps_log, demand)
                return _humanize(plan + f"(已校验: {_v_msg})", True, demand)
            else:
                # 校验没过: 诚实标注"自报完成但结果没真达成", 继续修(不冒充完成)
                obs_summary += f"\n[校验] LLM自报完成, 但结果没真达成: {_v_msg}. 继续修, 别装作完成."
                print(f"[coder] 自报完成但校验没过: {_v_msg[:80]}")
                continue
        # 失败→自学分支
        if not obs["ok"]:
            search_q = f"python {obs['stderr'][:150]}"
            # 源码兜底协议(dg-piagent式): 本仓库相关报错先注入本地代码地图, 上网搜索是最后手段
            local_hint = ""
            if "nono-mind" in demand or "本仓库" in demand or "器官" in demand:
                try:
                    from immune.codemap import build_map, to_digest
                    _digest = to_digest(build_map("/root/nono-mind"), max_lines=30)
                    local_hint = (f"\n[源码兜底] 本地真实代码结构(先核对这里, 不要猜API):\n{_digest}")
                except Exception:
                    pass
            search_res = _web_search(search_q)
            obs_summary += (f"\n[自学] 搜索'{search_q[:60]}' → {search_res[:400]}" + local_hint)
    # 步骤耗尽——但先做一次诚实复核(2026-09-13实测: step1已完成全部统计却因预算耗尽误报"未完成")
    # 复核规则: 最后一步成功 + LLM判断实际完成 → 判完成而非失败
    # 2026-09-30: 复核也过完成校验(防LLM自报完成), 未达成=诚实报未完成
    _last_ok = bool(steps_log and steps_log[-1].get("ok"))
    final = _llm([{"role": "user", "content":
        f"任务: {demand}\n执行了{budget}步。记录:\n{obs_summary}\n"
        f"判断: 任务实际上是否已经完成? 只答'已完成: <一句话结果>'或'未完成: <卡在哪>'"}])
    _rv_ok, _rv_msg = _verify_task(c, demand, last_ok_code or (steps_log[-1].get("code_len", 0) and "") , last_ok_lang or "python", task_id) if _last_ok else (False, "最后一步未成功")
    if _last_ok and ("已完成" in final and "未完成" not in final.split("\n")[0]) and _rv_ok:
        result = f"任务完成(预算复核, 已校验: {_rv_msg})。{final[:300]}"
        if task_id: c.task_finish(task_id, "done", result)
        from immune.evals import self_eval as _sev
        _sev(c, task_id, "coder", steps_log, demand)
        _tool_note = _maybe_tool_factory(last_ok_code, last_ok_lang, demand, task_id or "", c)
        return _humanize(result + (" " + _tool_note if _tool_note else ""), True, demand)
    if task_id: c.task_finish(task_id, "failed", final)
    return _humanize(f"未完成: {final[:300]}", False, demand)

run = track_progress("coder", lambda demand, c, task_id=None, difficulty="medium": "编程:" + str(demand)[:50])(run)   # 票1: 写states/coder/progress
