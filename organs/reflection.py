#!/usr/bin/env python3
"""反思器官: 诺诺的"睡觉做梦" + 系统自检执行者(器官协作v1)
触发: 混合式(每晚22:00定时兜底 + task_done事件触发 + task_forward协作请求)
动作: ①捞挂起区(第三重保险) ②总结当天对话/任务→wiki ③更新主人模型
     ④收到 task_forward{target:self_check} → 真正执行自检(状态/信号/错误/健康), 结果写outbox
设计: core-architecture-v1.md §15.1 / §4
"""
import json, time, os, sys
sys.path.insert(0, "/root/nono-mind")
from llm_client import deep_chat as chat
from immune.plugins import capabilities_from_manifest, track_progress

# 票1: 能力跟插件走——从 plugins/reflection/manifest.yaml 读
CAPABILITIES = capabilities_from_manifest("reflection")

WIKI_DIR = "/root/wikis/nono/journal"
LIGHT_MAX_TOKENS = 300
WECHAT_OUTBOX = "wechat_outbox"
ORG_HEALTH_REFLECTION_KV = "organs/health/reflection"
LAST_SELF_CHECK_KV = "reflection/last_self_check"
PENDING_NOTES_KV = "reflection/pending_notes"

def _today():
    return time.strftime("%Y-%m-%d")

def _ensure_dir():
    os.makedirs(WIKI_DIR, exist_ok=True)


def _push_outbox(client, text: str):
    """把结果投到wechat_outbox(由wechat_loop器官发给主人)"""
    try:
        client.kv_append(WECHAT_OUTBOX, {"to": "owner", "text": text})
    except Exception as e:
        print(f"[reflection] 结果写outbox失败: {e}")


def _parse_sig_payload(payload) -> dict:
    """signals表里的payload可能是JSON字符串且带data包装, 统一解成任务层dict"""
    p = payload
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            return {}
    if not isinstance(p, dict):
        return {}
    data = p.get("data")
    return data if isinstance(data, dict) else p


def _loop_error_text(row: dict) -> str:
    """从signals行里的loop_error提取可读错误"""
    try:
        p = json.loads(row.get("payload") or "{}")
    except Exception:
        p = {}
    if isinstance(p, dict) and isinstance(p.get("data"), dict):
        p = p["data"]
    err = str((p or {}).get("err") or "").strip()
    orig = str((p or {}).get("orig") or "").strip()
    if not err and not orig:
        return "(无错误详情)"
    return f"{orig}:{err[:80]}" if orig else err[:80]


def self_check(client, origin_text: str = "") -> str:
    """真正执行自检: 状态服务/可见信号/最近错误/器官健康/画布状态.
    结果写入wechat_outbox告诉主人, 并留痕 reflection/last_self_check + organs/health/reflection"""
    now = time.time()
    lines = [f"【自检报告 {time.strftime('%H:%M')}】reflection器官替主人做了一次真检查:"]
    problems = []
    state_ok = False
    try:
        r = client._req(op="ping")
        state_ok = bool(r.get("ok"))
        lines.append(f"- 状态服务: 正常 (revision={r.get('revision')})")
    except Exception as e:
        lines.append(f"- 状态服务: 异常 ({str(e)[:60]})")
        problems.append("状态服务异常")
    sigs = []
    try:
        sigs = client.signals() or []
    except Exception as e:
        problems.append(f"信号读取失败: {str(e)[:50]}")
    by_type = {}
    errs = []
    for s in sigs:
        t = str(s.get("sig_type") or "?")
        by_type[t] = by_type.get(t, 0) + 1
        if t == "loop_error":
            errs.append(_loop_error_text(s))
    if by_type:
        lines.append("- 可见信号: " + ", ".join(f"{k}×{v}" for k, v in sorted(by_type.items())))
    else:
        lines.append("- 可见信号: 暂无可读信号")
    if errs:
        lines.append(f"- 最近错误: {len(errs)}条loop_error → " + " ; ".join(errs[:2]))
        problems.append(f"发现{len(errs)}条loop_error")
    else:
        lines.append("- 最近错误: 无loop_error")
    try:
        last = client.kv_get("canvas/last_creation")
    except Exception:
        last = None
    if last:
        subject = str(last.get("subject") or "未知")[:20]
        when = time.strftime("%H:%M", time.localtime(float(last.get("at") or 0)))
        if last.get("ok"):
            lines.append(f"- 画布最近创作: 成功「{subject}」({when}, 推送{'成功' if last.get('pushed') else '失败'})")
        else:
            lines.append(f"- 画布最近创作: 失败「{subject}」({when}, {str(last.get('error') or '未知原因')[:40]})")
            problems.append("最近一次画布创作失败")
    else:
        lines.append("- 画布最近创作: 暂无")
    try:
        health = client.kv_list("organs/health/")
    except Exception:
        health = {}
    if health:
        parts = []
        for k in sorted(health):
            v = health[k] or {}
            organ = k.rsplit("/", 1)[-1]
            parts.append(f"{organ}:{'正常' if v.get('ok') else '异常'}")
        lines.append("- 器官健康上报: " + " · ".join(parts))
    else:
        lines.append("- 器官健康上报: 暂无")
    try:
        hist = client.kv_get(f"dialogue_history/{_today()}")
    except Exception:
        hist = None
    lines.append(f"- dialogue今日对话: {len(hist) if isinstance(hist, list) else 0}条")
    lines.append("结论: " + ("一切正常, 主人放心~[得意]" if not problems
                             else f"发现{len(problems)}处需要留意, 已记录, 我会继续盯着~"))
    msg = "\n".join(lines)
    try:
        client.kv_set(LAST_SELF_CHECK_KV, {"ok": not problems, "at": now,
                                           "problems": len(problems), "origin": origin_text[:120]})
        client.kv_set(ORG_HEALTH_REFLECTION_KV, {"ok": state_ok, "at": now,
                                                 "organ": "reflection", "role": "反思/自检"})
    except Exception as e:
        print(f"[reflection] 自检结果落State失败: {e}")
    _push_outbox(client, msg)
    return msg


def _absorb_task(client, target: str, origin: str) -> str:
    """v1尚无专属器官的任务: 原话进待办清单(reflection/pending_notes), 如实告知主人"""
    note = {"target": target or "note", "origin": (origin or "")[:300], "at": time.time()}
    try:
        notes = client.kv_get(PENDING_NOTES_KV) or []
        if not isinstance(notes, list):
            notes = []
        notes.append(note)
        client.kv_set(PENDING_NOTES_KV, notes[-20:])
    except Exception as e:
        print(f"[reflection] 待办记录失败: {e}")
    msg = (f"主人刚才说的「{(origin or '')[:60]}」这类请求, v1还没有能直接执行的专属器官, "
           "我已把原话记进待办清单, 等对应器官上线第一时间告诉主人~[认真]")
    _push_outbox(client, msg)
    return msg


def _handle_task_forward(payload, client) -> str:
    """器官协作v1: task_forward按target执行. self_check→真自检; 其他→吸收记录"""
    data = _parse_sig_payload(payload)
    target = str(data.get("target") or "")
    origin = str(data.get("origin_text") or "")
    if target == "self_check":
        return self_check(client, origin)
    return _absorb_task(client, target, origin)

def sweep_suspended(client):
    """捞起挂起区信号: 扫描suspended/前缀, 处理+清理(含TTL 7天过期)"""
    items = client.kv_list("suspended/")
    swept = []
    now = time.time()
    for key, item in items.items():
        ttl = item.get("ttl", 7*24*3600)
        if now - item.get("at", now) > ttl:
            client.kv_del(key)          # 过期丢弃
            continue
        # 升级处理: 挂起信号重新以正常通道进入系统(保留route_to字段, 修复P0-3)
        client.emit(item["type"],
                    {"data": item["payload"],
                     "route_to": "dialogue" if item["type"] == "master_message"
                                 else ("reflection" if item["type"] in ("task_done", "loop_error")
                                       else "state_only"),
                     "priority": 2},
                    decay=0.1, source="reflection_sweep")
        client.kv_del(key)
        swept.append(key)
    return swept

def summarize_day(client) -> str:
    """用DeepSeek总结当天对话+任务, 产出日记"""
    hist = client.get_history_range(days=1)
    tasks = client.kv_get("today_tasks") or []
    if not hist and not tasks:
        return ""
    text = "\n".join(f"主人: {h.get('content','')}\n诺诺: {h.get('reply','')}"
                     for h in hist[-20:] if isinstance(h, dict))
    if tasks:
        text += "\n\n今日任务: " + json.dumps(tasks, ensure_ascii=False)
    prompt = f"""以下是诺诺(nonon-mind MVP)今天的对话与任务记录. 请以诺诺的第一人称写一篇150字以内的"今日小结", 
格式: 学到了什么/做了什么/有什么想对主人说的. 直接输出小结正文, 不要标题.

记录:
{text}"""
    try:
        return chat([{"role": "user", "content": prompt}], max_tokens=400)
    except Exception as e:
        return f"(反思时LLM失败: {e})"

def _humanize_push(client, reply: str, force: bool = False):
    """重大成果主动报喜入口: coder等真出活 → 限频推主人.
    (9/29主人拍板: 闲时内心独白/反思日志禁用此入口——那属于后台, 不发主人微信.
    本入口只留给'真帮主人干完一件事'的报喜, 且文案用人话不独白腔.)"""
    last_push = (client.kv_get("last_proactive_push") or {}).get("at", 0)
    if not force and time.time() - last_push < 4 * 3600:
        return False
    text = f"主人~ 跟您说下: {reply[:150]}"
    client.kv_append("wechat_outbox", {"to": "owner", "text": text, "from_organ": "dialogue"})
    client.kv_set("last_proactive_push", {"at": time.time(), "note": reply[:100]})
    print(f"[反思] 主动分享已推: {text[:60]}")
    return True


def idle_reflect(client, use_llm=True) -> dict:
    """闲时轻反思(区别于22点全量日反思):
    只扫当天dialogue_history分片的增量部分(上次轻反思之后的新对话),
    单次LLM短调用提炼 → 追加到当日journal. 无新对话则不动作(不耗LLM).
    (9/29主人拍板: 闲时随笔只存记忆库, 不再推微信——曾把内心独白弹进聊天=人设分裂泄漏)"""
    _ensure_dir()
    day = _today()
    shard = client.kv_get(f"dialogue_history/{day}") or []
    cur = client.kv_get("idle_reflect_cursor") or {}
    seen = cur.get("count", 0) if cur.get("day") == day else 0
    if seen > len(shard):
        # P1-6: 分片被裁剪(上限200条)后旧游标越界 → 对齐到当前长度并落盘,
        # 轻反思后续不永久停摆(不回头重总结已被裁掉的旧内容)
        seen = len(shard)
        client.kv_set("idle_reflect_cursor", {"day": day, "count": seen, "at": time.time()})
    new_entries = shard[seen:] if len(shard) > seen else []
    if not new_entries:
        return {"date": day, "new_entries": 0, "journal": None}
    text = "\n".join(
        ("主人" if e.get("role") == "user" else "诺诺") + f": {e.get('content','')}"
        for e in new_entries[-12:] if isinstance(e, dict))
    note = ""
    if use_llm:
        try:
            note = chat([{"role": "user", "content":
                f"以下是诺诺刚才闲下来之前的一小段对话. 以第一人称用80字以内记一条闲时随笔: "
                f"刚才聊了什么/有什么发现或值得留意的. 直接输出正文, 不要标题.\n\n{text}"}],
                max_tokens=LIGHT_MAX_TOKENS)
        except Exception as e:
            note = f"(轻反思LLM失败: {e})"
    if not note:
        note = f"闲时又有{len(new_entries)}条新对话, 简单记一笔备晚上总结."
    path = os.path.join(WIKI_DIR, f"{day}.md")
    stamp = time.strftime("%H:%M")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(f"# 诺诺日记 {day}\n")
    with open(path, "a") as f:
        f.write(f"\n## 闲时随笔 {stamp}\n\n{note}\n")
    # 地基③④: 反思产物写回检索层(insights/), 带evidence指针——journal不再是孤岛
    try:
        from immune import trust
        insight_id = f"{day}_{stamp.replace(':', '')}"
        rec = trust.stamp({
            "at": time.time(), "note": note[:500],
            "evidence": f"dialogue_history/{day}[{seen}:{len(shard)}]",
            "importance": 3}, "INFERRED", evidence=f"dialogue_history/{day}")
        client.kv_set(f"insights/{insight_id}", rec)
    except Exception as e:
        print(f"[反思] insights写回失败: {e}")
    # 9/29主人拍板: 闲时随笔不再推微信(内心独白弹进聊天=人设分裂泄漏), 只存记忆库/日记
    client.kv_set("idle_reflect_cursor", {"day": day, "count": len(shard), "at": time.time()})
    # NOOA reflection pass: 每10次闲时反思跑一次memories/蒸馏(合并/剪枝/清死边)
    try:
        cur = client.kv_get("idle_reflect_cursor") or {}
        n = (cur.get("gc_count", 0)) + 1
        if n % 10 == 0:
            from immune.memory_gc import consolidate
            stats = consolidate(client)
            print(f"[反思] memories蒸馏: {stats}")
            cur["gc_count"] = 0
        else:
            cur["gc_count"] = n
        client.kv_set("idle_reflect_cursor", cur)
    except Exception as e:
        print(f"[反思] memories蒸馏失败: {e}")
    return {"date": day, "new_entries": len(new_entries), "journal": path}

def handle(sig_type, payload, client):
    """意识核 dispatch 入口(常驻loop内): 区分闲时轻反思与其他reflection信号"""
    if sig_type == "task_forward":
        # 器官协作v1: 自检/待吸收任务, 真正执行后结果写outbox
        r = _handle_task_forward(payload, client)
        head = r.splitlines()[0] if r else "(空结果)"
        print(f"[意识核] reflection执行task_forward → {head}")
    elif sig_type == "reflection_trigger":
        r = idle_reflect(client, use_llm=os.environ.get("REFLECTION_NO_LLM") != "1")
        print(f"[反思] 闲时轻反思完成: {r}")
    else:
        print(f"[意识核] → reflection吸收({sig_type})")

def update_master_model(client, summary: str):
    """主人模型增量更新: 追加今日观察到的主人特征"""
    model = client.kv_get("master_model") or {"traits": [], "updated": None}
    # MVP: 简单记录活跃度. 完整版: LLM提炼主人偏好/情绪/关心话题
    model["traits"] = list(set(model.get("traits", []) + ["daily_active"]))
    model["updated"] = _today()
    client.kv_set("master_model", model)

def run(client, deepseek=True) -> dict:
    """反思主流程. 返回结果摘要"""
    _ensure_dir()
    result = {"date": _today(), "suspended_swept": 0}
    # ① 挂起区
    result["suspended_swept"] = len(sweep_suspended(client))
    # ② 日记
    summary = summarize_day(client) if deepseek else "(跳过LLM)"
    if summary:
        path = os.path.join(WIKI_DIR, f"{_today()}.md")
        # P1-7: "w"覆写会把当天idle_reflect追加的闲时随笔整篇抹掉 → 改为追加,
        # 保留白天已有内容(文件不存在时先建标题行, 与idle_reflect的格式一致)
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(f"# 诺诺日记 {_today()}\n")
        with open(path, "a") as f:
            f.write(f"\n## 晚间总结 {time.strftime('%H:%M')}\n\n{summary}\n")
        result["journal"] = path
    # ③ 主人模型
    update_master_model(client, summary)
    # ④ 收尾: 通知意识核
    client.emit("task_done", {"organ": "reflection", "result": "daily_reflection_done"},
                decay=0.1, source="reflection")
    client.checkpoint()
    return result

# 票1: 进度互看
handle = track_progress("reflection", lambda sig_type, payload=None, client=None: str(sig_type))(handle)
run = track_progress("reflection", lambda client=None, deepseek=True: "每日反思")(run)

if __name__ == "__main__":
    from state.server import StateClient
    c = StateClient()
    r = run(c)
    print(json.dumps(r, ensure_ascii=False, indent=1))
