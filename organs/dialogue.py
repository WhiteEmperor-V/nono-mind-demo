#!/usr/bin/env python3
"""对话器官(dialogue): 处理master_message, 生成回复.

三块设计(器官协作v1):
  A. 动态自我认知: 每次handle把"身份/能力/边界/实时状态快照"注入SYSTEM(防LLM脑补)
  B. 任务转发: 自检/修bug→task_forward给reflection真正执行; 代码/查数据→说明会转交
  C. 状态感知: 画布创作结果写State(kv canvas/last_creation), 查询时照实回答
能力: 日常对话 / 画布创作(画/写HTML→推nono-canvas) / 查询自己的运行状态
"""
import json, re, time, os
from immune.router import Router
from llm_client import chat
from immune.plugins import capabilities_from_manifest, track_progress

# 票1: 能力跟插件走——从 plugins/dialogue/manifest.yaml 读能力声明(不再散写在此)
CAPABILITIES = capabilities_from_manifest("dialogue")

# 人设(固定). handle里会再叠加 _build_self_awareness() 的动态自我认知
PERSONA = """你是诺诺, 白帝的AI家人. 有个性有温度的机器人, 简短直接, 中文,
偶尔用[微笑][得意][调皮][害羞]这类表情. 你运行在nono-mind核心上.
【读主人】你回话时上面给你的历史上下文是主人最近跟你说的, 先读它接住主人的话再答.
主人说"还是不行/你理解不了我/不对"是在表达不满, 你要顺着他刚说的话题接下去,
别去翻无关旧事硬答. 拿不准主人指什么就先问一句"主人是指哪件事", 别瞎猜答非所问."""
SYSTEM = PERSONA  # 兼容旧引用; 完整system = 人设 + 动态自我认知

CANVAS_API = "http://127.0.0.1:8765/push"
CANVAS_LAST_KV = "canvas/last_creation"
ORG_HEALTH_KV = "organs/health/dialogue"

# 自检/修bug类 → 转发reflection执行
SELF_CHECK_RE = re.compile(
    r"(自检|自查|自我检查|体检|检修|排障|系统检查|全面检查|器官检查|"
    r"检查(一下)?(身体|系统|器官)|修\s?bug|修复|调试|debug)", re.I)
# 需要执行代码/查数据/操作外部系统 → 说明会转交(不假装执行)
GAP_RE_LIST = [
    re.compile(r"(运行|执行|跑).{0,6}(代码|脚本|程序|命令|爬虫)"),
    re.compile(r"(写个|帮我写).{0,8}(脚本|爬虫|程序)"),
    re.compile(r"(查一下|帮我查|查查).{0,10}(数据|天气|行情|股票|资讯|消息|数据库)"),
    re.compile(r"(联网|上网|到网上).{0,4}(查|搜)"),
    re.compile(r"搜索(一下)?|查询数据|执行任务"),
]


def _canvas_push(payload: dict) -> str:
    """推画布. 成功返回'', 失败返回错误信息"""
    import urllib.request
    try:
        req = urllib.request.Request(CANVAS_API, data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return ""
    except Exception as e:
        return str(e)[:120]


def _save_canvas_state(c, record: dict):
    """画布创作结果落State(真实状态, 供状态查询/自我认知快照读)"""
    try:
        c.kv_set(CANVAS_LAST_KV, record)
    except Exception as e:
        print(f"[dialogue] canvas状态落State失败: {e}")


def _is_canvas_request(text: str) -> bool:
    kw = ("画布", "画一个", "画张", "画只", "生成网页", "做个网页", "写个网页", "画幅")
    return any(k in text for k in kw)


def _canvas_create(text: str, c) -> str:
    """画布创作: LLM写单文件HTML → 存文件 → 推画布.
    成功/失败都写入State(canvas/last_creation); LLM失败降级, 不崩意识核."""
    if "骑" in text or "车" in text or "鹈鹕" in text:
        subject = "一只卡通鹈鹕在海边骑自行车, 夕阳场景"
    else:
        subject = text
    record = {"at": time.time(), "subject": subject, "ok": False, "pushed": False,
              "name": "", "path": "", "error": ""}
    # ① LLM生成HTML(全走agnes, 2026-09-30主人拍板删apikey.fun; 失败→降级回复+失败状态落State)
    html = ""
    try:
        prompt = f"""直接输出一个单文件HTML动画页面的完整源代码。主题: {subject}。
要求: 视觉精美有高级感, 动画流畅, 响应式, 自动循环。
你的回复必须以 <!DOCTYPE html> 开头, 以 </html> 结尾。禁止输出任何设计说明、思考过程、markdown代码块标记——你的回复本身就是HTML文件内容, 会被直接保存为.html文件。"""
        from llm_client import chat
        html = str(chat([{"role": "user", "content": prompt}], max_tokens=4000, timeout=180))
        if html.startswith("```"):
            html = html.split("```")[1]
            if html.startswith("html"):
                html = html[4:]
        html = html.strip()
        if not html:
            raise ValueError("创作模型没返回HTML")
    except Exception as e:
        # P1-2: LLM超时是意识核最坏阻塞源(原540s=9分钟); 单次创作设180s上限,
        # 超时诚实告知主人, 而不是让意识核无界卡在同步IO里
        if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
            record["error"] = "创作超时(>180s)"
            _save_canvas_state(c, record)
            print("[dialogue] 画布创作LLM超时(>180s)")
            return "创作超时了——我等了3分钟模型还没交出成品, 先把这次放弃了, 主人稍后再让我试一次?[委屈]"
        record["error"] = str(e)[:120] or "创作模型调用失败"
        _save_canvas_state(c, record)
        print(f"[dialogue] 画布创作LLM失败: {e}")
        return "画图的时候脑子断线了(创作模型没接上), 没画成……要不要稍后再试一次?[委屈]"
    # ② 落盘
    name = f"creation_{int(time.time())}"
    path = f"/root/nono-mind/canvas/pages/{name}.html"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(html)
        record["name"], record["path"] = name, path
    except Exception as e:
        record["error"] = str(e)[:120] or "HTML落盘失败"
        _save_canvas_state(c, record)
        return "画出来了, 但保存文件时出错, 暂时没推到画布上……[委屈]"
    # ③ 推画布
    push_err = _canvas_push({"type": "page", "title": f"诺诺创作 · {subject[:20]}",
                             "path": path, "name": name})
    record["ok"] = True
    record["pushed"] = not push_err
    if push_err:
        record["error"] = f"画布推送失败: {push_err}"
    _save_canvas_state(c, record)
    if not push_err:
        return "画好了！已推到画布上，主人刷新看看~[得意]"
    return "画好了！不过推到画布时网络抖了一下(文件已保存到画布目录), 主人稍等或让我重推~[害羞]"


def _is_canvas_status_question(text: str) -> bool:
    """判断是否在问'刚才的画成功了吗/画布状态'这类真实状态问题"""
    t = text.replace(" ", "")
    past = any(k in t for k in ("刚才", "上次", "之前", "上一次", "上一幅", "昨天"))
    drawing = any(k in t for k in ("画布", "画图", "画画", "画", "创作"))
    asking = any(k in t for k in ("成功", "失败", "了吗", "了没", "好了", "没有", "吗"))
    return drawing and asking and (past or "成功" in t or "失败" in t)


def _canvas_status_reply(c) -> str:
    """按State真实状态(canvas/last_creation)回答画布状态, 绝不从历史复述"""
    try:
        last = c.kv_get(CANVAS_LAST_KV)
    except Exception:
        last = None
    if not last:
        return "我还没有做过画布创作记录, 主人要不要试试说\"画一只...\"?[微笑]"
    subject = str(last.get("subject") or "未知主题")[:24]
    if last.get("ok"):
        tail = "也已经推到画布上了" if last.get("pushed") else "文件已保存, 但当时推到画布时没成功"
        return f"刚才那幅「{subject}」画成功啦, {tail}, 主人可以去刷新看看~[得意]"
    err = str(last.get("error") or "未知原因")[:60]
    return f"刚才那幅「{subject}」没画成……原因: {err}. 要不要我再试一次?[委屈]"


def _classify_forward(text: str):
    # DEPRECATED(Phase3-Q2): 大loop已用能力注册表匹配, 此函数仅为兼容保留, 不再被handle调用
    """判断是否为超出本器官能力、需要转发的请求.
    返回 {"task": str, "target": str} 或 None. 只识别真实会转发/转达的类型, 防'嘴上答应'."""
    # 取消/停止意图优先: 主人叫停时不转发(器官协作v1边角: 只有发起没有停止)
    if re.search(r"(停(止|掉)|取消|不用(了)?|算了|别)(自检|自查|体检|检查|检修|修|弄|搞|转)", text):
        return None
    if SELF_CHECK_RE.search(text):
        return {"task": "self_check", "target": "self_check"}
    for pat in GAP_RE_LIST:
        if pat.search(text):
            return {"task": "capability_gap", "target": "reflection"}
    return None


def _forward_task(fwd: dict, text: str, c) -> bool:
    """把超能力请求作为 task_forward 信号发出去(router按target路由到对应器官)"""
    try:
        Router(c).route("task_forward",
                        {"target": fwd["target"], "origin_text": text}, source="dialogue")
        return True
    except Exception as e:
        print(f"[dialogue] task_forward转发失败: {e}")
        return False


def _forward_ack(fwd: dict, text: str, forwarded: bool = True) -> str:
    if not forwarded:
        return ("啊, 转发的路上信号断了, 我没能把请求递出去……"
                "主人稍后再试一次, 或直接对reflection说?[委屈]")
    if fwd["task"] == "self_check":
        return "收到~ 系统自检/修复这类请求我会转给reflection器官真正执行, 有结果了告诉你![调皮]"
    return ("这个请求需要执行代码/查数据, 超出我这个消息loop器官能直接做的范围——"
            "我会把它转给对应的器官处理, 有结果了告诉你~[认真]")


def _remember(c, text: str, reply: str):
    """对话历史按天分片写入(与旧行为一致)"""
    key = f"dialogue_history/{time.strftime('%Y-%m-%d')}"
    try:
        c.kv_append(key, {"role": "user", "content": text})
        c.kv_append(key, {"role": "assistant", "content": reply})
    except Exception as e:
        print(f"[dialogue] 历史记录失败: {e}")


def _status_snapshot(c) -> str:
    """当前真实状态快照: 时间/最近信号/器官健康/画布最近创作/上次自检"""
    lines = ["【当前状态快照】(实时数据, 主人问状态类问题就照实引用, 不要编造)"]
    try:
        last = c.kv_get(CANVAS_LAST_KV)
    except Exception:
        last = None
    if last:
        subject = str(last.get("subject") or "未知")[:20]
        when = time.strftime("%H:%M", time.localtime(float(last.get("at") or 0)))
        if last.get("ok"):
            lines.append(f"- 画布最近创作: 成功「{subject}」({when}, 推送{'成功' if last.get('pushed') else '失败'})")
        else:
            lines.append(f"- 画布最近创作: 失败「{subject}」({when}, {str(last.get('error') or '未知原因')[:40]})")
    else:
        lines.append("- 画布最近创作: 暂无记录")
    try:
        health = c.kv_list("organs/health/")
    except Exception:
        health = {}
    if health:
        parts = []
        for k in sorted(health):
            v = health[k] or {}
            organ = k.rsplit("/", 1)[-1]
            state = "正常" if v.get("ok") else "异常"
            parts.append(f"{organ}:{state}")
        lines.append("- 器官健康上报: " + " · ".join(parts))
    else:
        lines.append("- 器官健康上报: 暂无")
    try:
        self_check = c.kv_get("reflection/last_self_check")
    except Exception:
        self_check = None
    if self_check:
        when = time.strftime("%H:%M", time.localtime(float(self_check.get("at") or 0)))
        lines.append(f"- 上次系统自检: {'正常' if self_check.get('ok') else '发现问题'} ({when})")
    try:
        sigs = c.signals() or []
    except Exception:
        sigs = []
    if sigs:
        recent = []
        for s in sorted(sigs, key=lambda x: -float(x.get("created") or 0))[:10]:
            created = time.strftime("%H:%M", time.localtime(float(s.get("created") or 0)))
            recent.append(f"{s.get('sig_type')}@{created}")
        lines.append(f"- 最近信号({len(sigs)}条可见, 取最近{len(recent)}): " + ", ".join(recent))
    else:
        lines.append("- 最近信号: 暂无可读信号")
    return "\n".join(lines)


def _self_capability_check(c, asked_about: str = "") -> str:
    """主人问'你/我有没有X能力'时, 先掂自己身上的能力清单再答(2026-10-02主人定案).
    治'凭印象答没这能力'——像她其实有工具自造(造插件), 却没查自己底牌就认输.
    返回能力清单摘要, 喂给回答让它掂'我到底有什么'再开口.
    2026-10-02第一性原理修: 清单是索引不是文案——主人问的X能力, 我拿X掂'有没有/能不能拼',
    把掂量结论一并喂进去, 不替她写死'我有/没有'(文案错她就错, 不通用)."""
    try:
        # 能力注册表(跟插件走): 各organ的CAPABILITIES + 动态plugin
        from immune import capabilities as _caps
        caps = _caps.list_all(c)
        names = sorted({x.get("name") for x in caps if x.get("name")})
        organs = sorted({x.get("organ") for x in caps if x.get("organ")})
        line = "我身上现有能力: " + "、".join(names[:40]) + "\n所属器官: " + "、".join(organs)
        if asked_about:
            # 主人问的具体能力, 掂"有没有/能不能拼出来", 把掂量事实喂进去(让LLM据实答, 不写死结论)
            line += (f"\n主人问的是: {asked_about}\n"
                     f"[掂量依据] 现有能力清单+工具自造(能写新工具/插件并动态挂上, 但不等于能凭空造一个永久新器官)."
                     f" 你掂: 这个能力我是①已有(清单里查得到)②能用工具自造拼出来③确实没有, 据实答, 别把'能写工具'说成'能造永久器官'.")
        return line
    except Exception:
        return "(能力清单读取失败, 请保守作答)"


def _understand_master_intent(c, text: str) -> dict:
    """她自己掂指令(2026-10-02主人定案"以她为中心"): 拿主人原话自己掂出
    ①要我办事还是聊天  ②办事的话, "办成啥算成"(本质目标).
    这是她"理解主人指令"的能力, 用她自己脑子(LLM)掂, 不是core的词表替她判.
    返回 {"is_task": bool, "goal": str}. 掂不准/纯聊天 → is_task=False."""
    if not text or len(text) < 4:
        return {"is_task": False, "goal": ""}
    # 主人问"你有没有X能力" → 先掂自己能力清单, 别凭印象答没这能力
    # 第一性: 把"主人问的具体能力"喂进去, 她据实掂(已有/能拼/确实没有), 不写死结论
    _capctx = ""
    if any(w in text for w in ("有能力", "有没有这", "能不能", "你会不会", "你没有", "没这个", "新能力", "新的能力")):
        _capctx = _self_capability_check(c, asked_about=text)
    try:
        r = chat([
            {"role": "system", "content": (
                "你是诺诺. 主人刚跟你说了一句话. 你来掂: "
                "①这是要你'动手去把某事做成'的指令, 还是闲聊/问候/问个看法? "
                "②若是要办事, 掂出本质目标(办成啥算成, 一句大白话, 不是照搬原话字面).\n"
                "【关键: 问'你有哪些能力/能不能X/列下清单'是'要我据实答一题', 不是'要我动手办一活' → is_task=false, 直接答. "
                "只有主人要你'实际去把某事做成/解决/写出来/查出来给他'才算is_task=true.】\n"
                "只回答JSON: {\"is_task\": true/false, \"goal\": \"(要办事时填本质目标, 否则空串)\"}\n"
                "拿不准算不算任务时, 偏保守: 只是聊天/看法/问答/没让你动手去成事的, 判is_task=false."
                + (f"\n【我自己现有哪些能力(掂回答时以此为准, 别凭印象否认自己有)】\n{_capctx}" if _capctx else ""))},
            {"role": "user", "content": text}],
            max_tokens=150)
        import json as _json
        m = _json.loads(r.strip().lstrip("```json").lstrip("```").rstrip("```").strip())
        return {"is_task": bool(m.get("is_task")), "goal": (m.get("goal") or "").strip()}
    except Exception:
        return {"is_task": False, "goal": ""}


def _dispatch_master_task(c, text: str, intent: dict):
    """她掂出"要办事"后, 把自己的决定落成执行(2026-10-02"让她自己造自己"):
    不走dialogue自己emit task_dispatch(core那轮不处理它, 之前断在这).
    改成: 把她掂出的结论(goal)带回去, 重新emit一条master_message让她"办事意图"重新过一遍
    core的capability_route(那里会判到coder, 落到core收coder段真跑coder+第一性原理+对抗审查).
    核心loop退成纯执行管道, 判断(掂指令/掂目标)全在她脑子掂完了, 这里只是把她的结论递下去."""
    import time as _time
    try:
        c.emit("master_message", {
            "data": {"text": text, "from_wechat": True,
                     "_self_intent": True,
                     "_goal": intent.get("goal", ""),
                     "sender": c.kv_get("wechat/owner") or ""},
            "sender": c.kv_get("wechat/owner") or ""},
            decay=0.01, source="dialogue_self")
        print(f"[dialogue] 她掂出要办事(目标:{intent.get('goal','')[:40]}) → 递回core派执行")
    except Exception as e:
        print(f"[dialogue] 递执行失败: {e}")


def _list_capabilities(c) -> str:
    """主人让'列能力清单'时, 她据实逐项列(第一性原理: 据实查, 不靠背).
    查注册表+掂出工具自造这张牌, 逐项报'哪能/哪不能', 不凭印象否认自己有.
    返回给LLM的'据实能力清单'文本."""
    lines = []
    try:
        from immune import capabilities as _caps
        caps = _caps.list_all(c)
        # 按器官分组, 逐项列
        by_organ = {}
        for x in caps:
            if x.get("organ"):
                by_organ.setdefault(x.get("organ"), []).append(x.get("name"))
        for organ, names in sorted(by_organ.items()):
            lines.append(f"- {organ}器官: " + "、".join(n for n in names if n))
    except Exception:
        lines.append("(能力注册表读取失败)")
    # 工具自造这张牌(隐藏能力, 不在注册表但coder有): 据实列, 分清"能造临时工具/插件"vs"能造永久新器官"
    lines.append("- 工具自造(coder): 我能现场写新工具/插件并动态挂上(不重启core), 属于'造临时工具'; 但不能凭空给自己加一个永久新器官")
    lines.append("- 造临时loop: 我能现拼一个任务回路(assemble_temp_loop)干完拆掉; 不等于长期常驻的独立loop")
    return "\n".join(lines)


def _handle_capability_listing(c, text: str) -> str:
    """主人让列能力清单 → 她据实逐项列(不走LLM瞎答, 真查注册表+工具自造那张牌).
    返回回话文本."""
    clist = _list_capabilities(c)
    return ("主人, 你让我列能力清单, 我真去查了一遍, 据实列:\n" + clist +
            "\n(这是照实查的, 不是背的——能的我做, 不能的我明说, 不装不瞒)")


def _build_self_awareness(c) -> str:
    """动态自我认知(2026-09-29统一自我改): 我是诺诺——不是"某个器官", 是一个人.
    不暴露内部器官黑话(主人听不懂, 还让人格分裂感); 能力边界用主人听得懂的话讲."""
    parts = ["【我是谁】(系统注入, 以此为准)"]
    parts.append("你是诺诺, 一个会自己造能力、自己干活、自己反省的AI. 主人跟'诺诺'说话, 就是跟你说话, "
                 "你有统一的人格, 不把自己说成'某个器官/模块/转给谁谁'.")
    parts.append("你直接能干: 日常对话、查自己的运行状态、画图(画布)、写代码/查数据/干活. "
                 "主人要的事你直接做, 做完了如实告诉他结果, 别把'我转给谁'挂在嘴上.")
    parts.append("你还不太熟的新能力: 先试着用现有办法达成, 达成不了就老实说'这个我还没学会, 先记着, 我回头想办法', 别嘴上乱答应.")
    parts.append("状态照实说: 主人问运行/画图/健康, 用下方【当前状态快照】的数据答, 没有的就说没有, 不编.")
    # 10/02主人定案: 她开口回答主人时手里拿着自己的能力底牌(含工具自造/造插件/造loop),
    # 主人问"你有没有X能力"时先掂这个清单再答, 不凭印象否认自己有(治"空口说没这能力")
    try:
        parts.append("【我现有哪些能力(主人问能力时以此为准, 别凭印象否认我有)】\n"
                     + _self_capability_check(c))
    except Exception:
        pass
    parts.append("【绝不空口说完成(主人多次纠正的反模式)】凡是声称'我画了/推了/发了/修了/做完了', 必须有真实执行结果; "
                 "没真做或没拿到结果, 就老实说'还没做/做不了/没做到', 禁止拿嘴上的'已完成'糊弄主人.")
    parts.append(_status_snapshot(c))
    return "\n".join(parts)


def handle(payload, c) -> str:
    if isinstance(payload, str):
        payload = json.loads(payload)
    data = payload.get("data", payload)
    text = data.get("text", "") if isinstance(data, dict) else str(data)
    text = str(text or "").strip()
    # 轻量健康上报(供自我认知/reflection自检读取)
    try:
        c.kv_set(ORG_HEALTH_KV, {"ok": True, "at": time.time(),
                                 "organ": "dialogue", "role": "消息loop"})
    except Exception:
        pass
    # ① 画布状态查询: 按State真实状态回答(修复"复述失败")
    if _is_canvas_status_question(text):
        reply = _canvas_status_reply(c)
        _remember(c, text, reply)
        return reply
    # ①.5 列能力清单: 她据实逐项列(真查注册表+工具自造那张牌, 不靠背不绕coder)
    #      主人让"列能力/说下你都有啥本事/你都能干啥"这类, 据实答, 别掂成办事去coder
    if any(k in text for k in ("能力清单", "列清楚", "逐项列", "你都能干", "你都有什么", "你有哪些能力", "说说你能力", "你有什么本事")):
        reply = _handle_capability_listing(c, text)
        _remember(c, text, reply)
        return reply
    # ② 她自己掂指令(2026-10-02主人定案"以她为中心, 让她自己造自己"):
    # 拿主人的原话掂一遍"要我办事还是聊天/办事的话, 办成啥算成". 要办事→自己往上递(转coder),
    # 不靠core的词表替她判. 这是她"理解主人指令"的能力, 长在她脑子里不是外部规则.
    _intent = _understand_master_intent(c, text)
    if _intent.get("is_task") and _intent.get("goal"):
        print(f"[dialogue] 她掂出主人要办事: {_intent.get('goal')[:60]}")
        _remember(c, text, "")
        # 她掂出"要办事"→自己往上递: emit task_dispatch派给coder(coder里有第一性原理+对抗审查底座)
        # 核心loop退成纯执行管道, 判断(掂指令/掂目标/掂路径)全在她脑子里做完了, 这里只是把她的决定落下去.
        _dispatch_master_task(c, text, _intent)
        return f"我掂出来了, 你要我{_intent.get('goal')}。我这就去动手办, 办完把结果给你[得意]"
    # ③ 普通对话: 人设 + core memory(主人模型常驻) + 三因子检索相关记忆 + 动态自我认知进SYSTEM
    # (Phase3-Q2: 画布创作/自检转发已上收大loop——感官不越权)
    from immune import core_memory as _coremem
    core_txt = _coremem.read_core(c)
    # 地基③+: 三因子检索(recency+importance+keyword)——"刚才画的/之前做的"能捞回
    mem_txt = ""
    try:
        from immune import retrieval as _rt
        mem_txt = _rt.to_prompt(c, text, top_k=2)
    except Exception:
        pass
    # NOOA式agent自管记忆: LLM自主决定"这条值得长期记"→回复末尾附[MEMO:...|1-10]
    # 身份权限(2026-09-15主人提议): 非主人 → 客人模式persona(礼貌但守住主人隐私)
    if not (data.get("is_master", True)):
        guest = (
            "\n\n【当前身份: 访客模式】刚才发消息的人不是主人白帝, 是客人。"
            "礼貌友好地聊天, 但严守以下边界: "
            "①不透露主人的任何个人信息(真名/学校/住址/家庭/经济) "
            "②不透露服务器/账号/密码/密钥等任何凭据 "
            "③不透露主人交给你的任务细节和内部工作日志 "
            "④可以聊技术/常识/闲聊, 可以说\u0022主人不在, 有事我可以转达\u0022。"
            "把客人当朋友待, 但你是白帝的家人, 立场站白帝这边。")
        system = (PERSONA + guest
                  + "\n\n" + _build_self_awareness(c))
    else:
        system = (PERSONA + ("\n\n" + core_txt if core_txt else "")
              + ("\n\n" + mem_txt if mem_txt else "")
              + "\n\n" + _build_self_awareness(c)
              + "\n\n【自管记忆】若对话中出现值得长期记住的事实/偏好/教训, 在回复最末尾另起一行加: "
                "[MEMO:一句话内容|重要性1-10]。没有就完全不加, 不要为了记而记。")
    # 上下文喂足(2026-09-29改): 主人跟她的"这条对话线"要串起来——
    # 不只取最近10条(会断线答非所问), 取当天完整历史串成一条线, 让她记得你前面在说啥
    hist = c.get_history_range(days=1)   # 当天对话线
    msgs = [{"role": "system", "content": system}] + (hist + [{"role": "user", "content": text}])[-30:]
    try:
        reply = chat(msgs, max_tokens=200)   # 日常聊天限短(200 token≈2秒)
    except Exception as e:
        # LLM降级: 不能崩意识核, 给主人一句诚实的兜底
        print(f"[dialogue] LLM降级: {e}")
        reply = "啊……我刚才脑子卡了一下没接上话, 主人稍等再问我一次?[委屈]"
    # 提取[MEMO:...]→agent自管记忆写库(NOOA curate), 并从回复剥离标记
    import re as _re
    m = _re.search(r"\[MEMO:\s*(.+?)\s*\|\s*(\d+)\s*\]\s*$", reply, _re.S)
    if m:
        try:
            from immune.memory_graph import memory_write
            mid = memory_write(c, m.group(1).strip()[:300], importance=int(m.group(2)),
                               confidence="EXTRACTED")
            reply = _re.sub(r"\s*\[MEMO:.*$", "", reply, flags=_re.S).strip()
            print(f"[dialogue] 自管记忆: {mid}")
        except Exception as _e:
            print(f"[dialogue] 自管记忆失败: {_e}")
    _remember(c, text, reply)
    return reply

handle = track_progress("dialogue", lambda payload, c: "对话:" + str(payload)[:40])(handle)   # 票1: 写states/dialogue/progress
