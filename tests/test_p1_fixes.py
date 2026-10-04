#!/usr/bin/env python3
"""P1修复回归自检(CODE_REVIEW_20260909): 全程无State socket, 沙箱可跑
P1-1 P0信号豁免衰减删除(只受TTL/主动drop约束)
P1-2 画布创作LLM超时180s → 诚实回复
P1-3 outbox失败重试计数, 超过上限放弃写log
P1-4 getupdates先处理成功再落sync_buf
P1-6 dialogue_history单日上限裁剪
P1-7 日反思追加写入, 不覆写闲时随笔
运行: /usr/bin/python3 tests/test_p1_fixes.py
"""
import asyncio
import copy, json, os, sys, shutil, tempfile, time, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PASS, FAIL = "✅", "❌"
results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{PASS if cond else FAIL} {name}" + (f" ({detail})" if detail else ""))


def scratch_dir(prefix):
    """测试临时目录放仓库内tests/下(沙箱禁止产物落/tmp), 用完即删."""
    return tempfile.mkdtemp(prefix=prefix, dir=os.path.join(ROOT, "tests"))


class MemState:
    """StateClient兼容替身(单进程内存实现, 供无socket的器官逻辑自检)."""
    def __init__(self):
        self.kv = {}
        self.sigs = {}
        self.rev = 0

    def _req(self, op="ping", **kw):
        return {"ok": True, "revision": self.rev, "request_id": kw.get("request_id")}

    def kv_set(self, key, value, expected_revision=None):
        self.rev += 1
        self.kv[key] = copy.deepcopy(value)
        return {"ok": True, "revision": self.rev}

    def kv_get(self, key):
        return copy.deepcopy(self.kv.get(key))

    def kv_append(self, key, item):
        arr = self.kv.get(key)
        if arr is None:
            arr = []
        elif not isinstance(arr, list):
            raise RuntimeError("value不是JSON数组")
        arr = list(arr)
        arr.append(copy.deepcopy(item))
        self.kv[key] = arr
        self.rev += 1
        return {"ok": True, "revision": self.rev}

    def kv_list(self, prefix=""):
        return {k: copy.deepcopy(v) for k, v in self.kv.items() if k.startswith(prefix)}

    def kv_del(self, key):
        self.kv.pop(key, None)
        return {"ok": True}

    def emit(self, sig_type, payload, strength=1.0, decay=0.3, source="", sig_id=None):
        sig_id = sig_id or uuid.uuid4().hex[:12]
        self.sigs[sig_id] = {
            "sig_id": sig_id, "sig_type": sig_type, "payload": copy.deepcopy(payload),
            "strength": strength, "decay": decay, "created": time.time(), "source": source,
        }
        return {"ok": True, "sig_id": sig_id}

    def signals(self):
        return list(self.sigs.values())

    def attention(self, focus_task=None):
        sigs = self.signals()
        sigs.sort(key=lambda s: -s["strength"])
        return sigs

    def drop(self, sig_id):
        self.sigs.pop(sig_id, None)
        return {"ok": True}

    def checkpoint(self):
        return {"ok": True}

# --------------------------------------------------------------------------
# P1-1: P0信号(master_message/risk_trigger)豁免逐tick衰减/物理删除,
# 只受P0_TTL与主动drop约束 —— 意识核阻塞2-9分钟不再丢主人消息
# --------------------------------------------------------------------------
def p1_1_p0_exempt_from_strength_decay():
    from state.model import Signal, P0_TTL
    from state.server import StateServer
    d = scratch_dir("p1_1_")
    srv = None
    try:
        srv = StateServer(db_path=os.path.join(d, "s.db"), autobind=False)

        def add(sig_type, created=None, decay=0.3):
            sig = Signal(sig_type, {}, created=created or time.time(), decay=decay)
            with srv._wlock:
                srv._apply("emit", {"sig": sig})
            return sig.sig_id

        mid = add("master_message", created=time.time() - 600)   # 10分钟前的主消息
        old = add("risk_trigger", created=time.time() - P0_TTL - 60)  # 超24h TTL
        nid = add("probe_decay")                                  # 普通信号对照
        tid = add("task_done")                                    # 防饿死保底语义对照
        for _ in range(250):      # ~250×0.5s=125s, 旧逻辑P0此刻已被物理删除
            srv._decay()
        rows = {r["sig_id"]: r for r in srv.conn.execute("SELECT * FROM signals").fetchall()}
        cur = (rows[mid]["strength"] if mid in rows else None)
        check("P1-1 P0消息125s后仍存在且满强度", mid in rows and cur == 1.0, f"strength={cur}")
        check("P1-1 超TTL的P0被物理清除", old not in rows)
        check("P1-1 普通信号仍按衰减删除", nid not in rows)
        check("P1-1 task_done防饿死保底未破坏", tid in rows,
              f"strength={rows[tid]['strength'] if tid in rows else 'gone'}")
        vis = srv.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE sig_type='master_message' AND strength>0.25"
        ).fetchone()[0]
        check("P1-1 P0仍在可见阈值之上(意识核恢复即可拾取)", vis == 1)
    finally:
        if srv is not None:
            srv.conn.close()
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# P1-2: 画布创作LLM调用超时上限540s→180s, 超时返回诚实回复并落State
# --------------------------------------------------------------------------
def p1_2_canvas_llm_timeout():
    import urllib.error
    import urllib.request
    import organs.dialogue as dg
    c = MemState()
    orig = urllib.request.urlopen
    seen = {}

    def boom(req, timeout=0):
        seen["timeout"] = timeout
        raise TimeoutError("timed out")

    try:
        urllib.request.urlopen = boom
        reply = dg._canvas_create("画一只猫", c)
        check("P1-2 超时返回诚实回复(含'超时')", "超时" in reply, reply[:40])
        rec = c.kv_get(dg.CANVAS_LAST_KV) or {}
        check("P1-2 超时落State(ok=False, error含超时)",
              rec.get("ok") is False and "超时" in str(rec.get("error") or ""), str(rec)[:60])
        check("P1-2 LLM调用timeout=180s(原540s)", seen.get("timeout") == 180,
              f"timeout={seen.get('timeout')}")
        # urllib真实超时常包成URLError(reason=TimeoutError), 同样走诚实分支
        def boom_wrapped(req, timeout=0):
            raise urllib.error.URLError(TimeoutError("timed out"))
        urllib.request.urlopen = boom_wrapped
        reply2 = dg._canvas_create("画一只猫", c)
        check("P1-2 URLError包裹的超时也返回超时回复", "超时" in reply2, reply2[:40])
    finally:
        urllib.request.urlopen = orig


# --------------------------------------------------------------------------
# P1-3: outbox失败项带retry计数, 连续失败超上限(3次)放弃写log, 不再无限重试
# --------------------------------------------------------------------------
def p1_3_outbox_retry_cap():
    import organs.wechat_loop as wl
    d = scratch_dir("p1_3_")
    try:
        c = MemState()
        organ = wl.WeChatLoopOrgan(client=c, data_dir=d, transport=None)
        organ._owner = "wxid_owner"

        async def always_fail(to, text, context_token=None):
            raise RuntimeError("send fail")
        organ._send_text = always_fail

        async def drain():
            return await organ._drain_outbox()

        c.kv_append(wl.STATE_OUTBOX_KEY, {"text": "发不出去的消息", "to": "wxid_owner"})
        for _ in range(3):
            asyncio.run(drain())
        remaining = c.kv_get(wl.STATE_OUTBOX_KEY) or []
        check("P1-3 前3次失败后条目保留且retry递增",
              len(remaining) == 1 and remaining[0].get("retry") == 3,
              json.dumps(remaining, ensure_ascii=False))
        r4 = asyncio.run(drain())
        remaining = c.kv_get(wl.STATE_OUTBOX_KEY) or []
        check("P1-3 超过上限的失败项被放弃(不再无限重试)",
              r4.get("dropped") == 1 and remaining == [],
              f"dropped={r4.get('dropped')} remaining={remaining}")
        for _ in range(3):   # 已放弃后队列为空, 不再产生任何发送尝试
            asyncio.run(drain())
        check("P1-3 放弃后outbox保持为空", (c.kv_get(wl.STATE_OUTBOX_KEY) or []) == [])

        # 无发送目标且未设主人: 保留待发但不消耗retry计数(仍不应被误弃)
        c2 = MemState()
        organ2 = wl.WeChatLoopOrgan(client=c2, data_dir=d, transport=None)
        organ2._send_text = always_fail
        c2.kv_append(wl.STATE_OUTBOX_KEY, {"text": "没主人的消息"})
        asyncio.run(organ2._drain_outbox())
        left = c2.kv_get(wl.STATE_OUTBOX_KEY) or []
        check("P1-3 无目标待发条目不因无owner被丢弃且不加retry",
              len(left) == 1 and "retry" not in left[0], json.dumps(left, ensure_ascii=False))
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# P1-4: getupdates先完整处理消息, 成功后才推进并持久化sync_buf
# (崩溃时旧buf未落盘→重启重复收(去重兜底), 而不是永久丢失)
# --------------------------------------------------------------------------
def p1_4_sync_buf_persist_after_process():
    import organs.wechat_loop as wl
    d = scratch_dir("p1_4_")
    try:
        with open(os.path.join(d, "account.json"), "w", encoding="utf-8") as f:
            json.dump({"ilink_bot_id": "bot_nono_1", "bot_token": "t-secret",
                       "base_url": "https://ilink.example", "ilink_user_id": "wxid_nono"}, f)

        class Tr:
            def __init__(self):
                self.n = 0

            async def post_json(self, url, *, headers, payload, timeout_ms):
                self.n += 1
                return {"ret": 0, "msgs": [
                    {"from_user_id": "wxid_x", "msg_type": 1,
                     "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
                     "message_id": f"m{self.n}"}],
                    "get_updates_buf": f"buf-{self.n}"}

            async def get_json(self, *a, **k):
                raise AssertionError("不应有GET")

        # 处理抛异常: buf不得推进, 更不得落盘(崩溃现场)
        tr1 = Tr()
        organ1 = wl.WeChatLoopOrgan(client=MemState(), data_dir=d, transport=tr1)

        def broken(msgs):
            raise RuntimeError("process boom")
        organ1._process_msgs = broken
        try:
            asyncio.run(organ1._poll_inbox_once())
            raised = False
        except RuntimeError:
            raised = True
        sync_saved = ""
        try:
            sync_saved = str(json.load(open(os.path.join(d, "sync.json"), encoding="utf-8"))
                             .get("get_updates_buf", ""))
        except Exception:
            pass
        check("P1-4 处理异常时新buf未推进且未落盘",
              raised and organ1._sync_buf == "" and sync_saved != "buf-1",
              f"sync_buf={organ1._sync_buf!r} saved={sync_saved!r}")

        # 处理成功: 消息进State且buf在成功后推进+落盘
        tr2 = Tr()
        organ2 = wl.WeChatLoopOrgan(client=MemState(), data_dir=d, transport=tr2)
        r = asyncio.run(organ2._poll_inbox_once())
        saved = json.load(open(os.path.join(d, "sync.json"), encoding="utf-8"))
        check("P1-4 成功处理后消息被处理", r["handled"] == 1, str(r))
        check("P1-4 成功后才推进并落盘sync_buf",
              organ2._sync_buf == "buf-1" and saved.get("get_updates_buf") == "buf-1",
              f"sync_buf={organ2._sync_buf!r} saved={saved!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# P1-6: dialogue_history单日无上限膨胀 → kv_append限长200条(超则裁到最近100);
# 裁剪后reflection的idle游标越界需重新基线, 轻反思不能永久停摆
# --------------------------------------------------------------------------
def p1_6_history_cap_and_idle_cursor():
    from state.model import Signal
    from state.server import StateServer
    import organs.reflection as rf
    d = scratch_dir("p1_6_")
    srv = None
    try:
        srv = StateServer(db_path=os.path.join(d, "s.db"), autobind=False)
        key = "dialogue_history/2099-01-01"
        for i in range(1, 202):     # 第201次append触发裁剪(>200 → 最近100)
            with srv._wlock:
                srv._apply("kv_append", {"key": key, "item": {"role": "user",
                                                              "content": f"msg-{i}"}})
        arr = json.loads(srv.conn.execute(
            "SELECT value FROM kv WHERE key=?", (key,)).fetchone()[0])
        check("P1-6 第201条后裁剪到最近100条", len(arr) == 100
              and arr[0]["content"] == "msg-102" and arr[-1]["content"] == "msg-201",
              f"len={len(arr)} head={arr[0]['content']} tail={arr[-1]['content']}")
        for i in range(202, 206):   # 继续追加只在上限内滚动, 不会回到无界
            with srv._wlock:
                srv._apply("kv_append", {"key": key, "item": {"role": "user",
                                                              "content": f"msg-{i}"}})
        arr = json.loads(srv.conn.execute(
            "SELECT value FROM kv WHERE key=?", (key,)).fetchone()[0])
        check("P1-6 后续追加不超过200条上限且尾部最新", len(arr) <= 200
              and arr[-1]["content"] == "msg-205", f"len={len(arr)}")
        for i in range(1, 251):     # 非dialogue key不受裁剪影响(通用append语义不变)
            with srv._wlock:
                srv._apply("kv_append", {"key": "wechat_outbox",
                                         "item": {"text": f"x{i}"}})
        raw = json.loads(srv.conn.execute(
            "SELECT value FROM kv WHERE key='wechat_outbox'").fetchone()[0])
        check("P1-6 非dialogue key不裁剪", len(raw) == 250, f"len={len(raw)}")
    finally:
        if srv is not None:
            srv.conn.close()
        shutil.rmtree(d, ignore_errors=True)

    # idle游标越界兜底: 裁剪后cursor.count>len(shard) → 对齐当前长度, 不永久停摆
    jd = scratch_dir("p1_6_journal_")
    old_wiki = rf.WIKI_DIR
    try:
        rf.WIKI_DIR = jd
        c = MemState()
        day = time.strftime("%Y-%m-%d")
        c.kv_set(f"dialogue_history/{day}", [{"role": "user", "content": f"c{i}"}
                                             for i in range(60)])
        c.kv_set("idle_reflect_cursor", {"day": day, "count": 300, "at": time.time()})
        r = rf.idle_reflect(c, use_llm=False)
        cur = c.kv_get("idle_reflect_cursor") or {}
        check("P1-6 越界游标对齐当前长度(不重总结)",
              r.get("new_entries") == 0 and cur.get("count") == 60,
              f"new={r.get('new_entries')} cursor={cur.get('count')}")
        path = os.path.join(rf.WIKI_DIR, f"{day}.md")
        check("P1-6 对齐后轻反思不误写journal", not os.path.exists(path))
        # 对齐之后继续追加新消息 → 轻反思恢复增量工作(不永久停摆)
        c.kv_append(f"dialogue_history/{day}", {"role": "user", "content": "裁剪后的新消息"})
        c.kv_append(f"dialogue_history/{day}", {"role": "assistant", "content": "收到"})
        r2 = rf.idle_reflect(c, use_llm=False)
        cur2 = c.kv_get("idle_reflect_cursor") or {}
        check("P1-6 越界对齐后轻反思恢复增量",
              r2.get("new_entries") == 2 and cur2.get("count") == 62,
              f"new={r2.get('new_entries')} cursor={cur2.get('count')}")
    finally:
        rf.WIKI_DIR = old_wiki
        shutil.rmtree(jd, ignore_errors=True)


# --------------------------------------------------------------------------
# P1-7: 日反思run()由"w"覆写改为追加——不抹掉白天idle_reflect的闲时随笔
# --------------------------------------------------------------------------
def p1_7_daily_reflection_appends():
    import organs.reflection as rf
    d = scratch_dir("p1_7_")
    old_wiki = rf.WIKI_DIR
    try:
        rf.WIKI_DIR = d
        day = time.strftime("%Y-%m-%d")
        path = os.path.join(d, f"{day}.md")
        # 白天已有闲时随笔(旧实现run()时会被"w"整篇抹掉)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 诺诺日记 {day}\n\n## 闲时随笔 10:00\n\n白天随手记的一笔。\n")
        c = MemState()
        r = rf.run(c, deepseek=False)
        content = open(path, encoding="utf-8").read()
        check("P1-7 日反思追加后保留白天闲时随笔", "白天随手记的一笔。" in content, content[-80:])
        check("P1-7 晚间总结已追加写入", "## 晚间总结" in content and "(跳过LLM)" in content,
              content[-80:])
        check("P1-7 run()结果正常返回journal", r.get("journal") == path and
              r.get("date") == day, str(r))
        # 无文件的全新日期: 先建标题行再追加, 标题不重复
        day2 = "2099-12-31"
        c2 = MemState()
        rf.WIKI_DIR = d
        os.environ["TZ"] = ""   # 保持_today()与day2一致由调用方保证
        import organs.reflection as _rf
        # 直接验证append逻辑: 模拟run在新文件上的写入结果
        path2 = os.path.join(d, f"{day2}.md")
        orig_today = _rf._today
        _rf._today = lambda: day2
        try:
            _rf.run(c2, deepseek=False)
        finally:
            _rf._today = orig_today
        content2 = open(path2, encoding="utf-8").read()
        check("P1-7 新文件先建标题再追加(标题不重复)",
              content2.count(f"# 诺诺日记 {day2}") == 1 and "## 晚间总结" in content2,
              content2[-80:])
    finally:
        rf.WIKI_DIR = old_wiki
        shutil.rmtree(d, ignore_errors=True)


def main():
    p1_1_p0_exempt_from_strength_decay()
    p1_2_canvas_llm_timeout()
    p1_3_outbox_retry_cap()
    p1_4_sync_buf_persist_after_process()
    p1_6_history_cap_and_idle_cursor()
    p1_7_daily_reflection_appends()
    print(f"\n{'=' * 40}")
    p = sum(1 for _, ok in results if ok)
    print(f"P1自检: {p}/{len(results)} 通过")
    sys.exit(0 if p == len(results) else 1)


if __name__ == "__main__":
    main()
