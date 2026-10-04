#!/usr/bin/env python3
"""wechat_loop 器官集成测试

覆盖:
  T1 依赖/结构: MANIFEST 与四件套函数存在
  T2 入站: mock getupdates -> 文本消息进 State(master_message, from_wechat=True)
  T3 去重: 同 message_id 重复投递只进一次
  T4 出站: 往 wechat_outbox append -> sendmessage 发出(echo context_token)
  T5 主动说话: 无 to 字段时发给主人(State wechat/owner)
  T6 会话失效: sendmessage errcode=-14 -> 去掉 context_token 重试成功
  T7 登录: mock get_bot_qrcode/get_qrcode_status -> account.json 落盘

State 后端:
  * 优先连真实 State 服务(Unix socket, 与 tests/test_state.py 一致)
  * 连不上(沙箱/服务未启动)时回退到同接口的内存替身, 断言逻辑不缩水
运行: /usr/bin/python3 tests/test_wechat_loop.py
"""
import asyncio
import copy
import json
import os
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import organs.wechat_loop as wl
from organs.wechat_loop import WeChatLoopOrgan, STATE_OUTBOX_KEY, STATE_OWNER_KEY

PASS, FAIL = "✅", "❌"
results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{PASS if cond else FAIL} {name}" + (f" ({detail})" if detail else ""))


def payload_of(sig):
    p = sig.get("payload")
    return json.loads(p) if isinstance(p, str) else p


# --------------------------------------------------------------------------
# State 后端
# --------------------------------------------------------------------------
class MemState:
    """StateClient 兼容替身(连不上真实服务时使用, 单进程内存实现)."""
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
        v = self.kv.get(key)
        return copy.deepcopy(v)

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

    def kv_del(self, key):
        self.kv.pop(key, None)
        return {"ok": True}

    def kv_list(self, prefix=""):
        return {k: copy.deepcopy(v) for k, v in self.kv.items() if k.startswith(prefix)}

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

    def close(self):
        pass


def make_state():
    """优先真实 State 服务; 失败回退内存替身. 返回 (client, is_real)."""
    try:
        from state.server import StateClient
        c = StateClient(max_retries=2)
        if c._req(op="ping").get("ok"):
            return c, True
        c.close()
    except Exception:
        pass
    return MemState(), False


STATE, STATE_IS_REAL = make_state()
print(f"[State后端] {'真实State服务' if STATE_IS_REAL else '内存替身(State服务不可达)'}")


def master_signals(client, text=None):
    out = []
    for s in client.signals():
        if s["sig_type"] != "master_message":
            continue
        d = payload_of(s).get("data") or {}
        if text is None or d.get("text") == text:
            out.append(s)
    return out


def cleanup_state(client):
    for s in client.signals():
        try:
            client.drop(s["sig_id"])
        except Exception:
            pass
    for k in (STATE_OWNER_KEY, STATE_OUTBOX_KEY, "wechat/tokens"):
        try:
            client.kv_del(k)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 假 transport(mock getupdates / sendmessage / 二维码接口)
# --------------------------------------------------------------------------
class FakeTransport:
    def __init__(self):
        self.updates = []          # list 按序弹出; 空了返回空批
        self.send_error_queue = []  # sendmessage 错误响应队列
        self.get_handlers = {}     # url 片段 -> 响应
        self.post_calls = []
        self.get_calls = []

    # 每轮 getupdates 之后需要拿到新 buf 供下轮 echo
    async def post_json(self, url, *, headers, payload, timeout_ms):
        self.post_calls.append((url, dict(headers), copy.deepcopy(payload)))
        if url.endswith("/" + wl.EP_GET_UPDATES):
            if self.updates:
                resp = self.updates.pop(0)
            else:
                resp = {"ret": 0, "msgs": [], "get_updates_buf": ""}
            return resp
        if url.endswith("/" + wl.EP_SEND_MESSAGE):
            if self.send_error_queue:
                return self.send_error_queue.pop(0)
            return {"ret": 0, "errcode": 0}
        raise AssertionError(f"意外的 POST: {url}")

    async def get_json(self, url, *, headers, timeout_ms):
        self.get_calls.append((url, dict(headers)))
        for frag, resp in self.get_handlers.items():
            if frag in url:
                return copy.deepcopy(resp)
        raise AssertionError(f"意外的 GET: {url}")


def empty_updates(buf=""):
    return {"ret": 0, "msgs": [], "get_updates_buf": buf}


def text_msg(sender, text, message_id="", context_token="", msg_type=1, **extra):
    msg = {
        "from_user_id": sender,
        "msg_type": msg_type,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if message_id:
        msg["message_id"] = message_id
    if context_token:
        msg["context_token"] = context_token
    msg.update(extra)
    return msg


def make_organ(tmpdir, state, transport, account=None):
    acct = account or {
        "ilink_bot_id": "bot_nono_1",
        "bot_token": "t-secret",
        "base_url": "https://ilink.example",
        "ilink_user_id": "wxid_nono",
    }
    with open(os.path.join(tmpdir, "account.json"), "w", encoding="utf-8") as f:
        json.dump(acct, f, ensure_ascii=False)
    organ = WeChatLoopOrgan(client=state, data_dir=tmpdir, transport=transport)
    return organ


# --------------------------------------------------------------------------
# T1 插件规格
# --------------------------------------------------------------------------
def t01_spec():
    mf = wl.MANIFEST
    check("T1 MANIFEST", mf.get("name") == "wechat_loop" and mf.get("loop_type") == "continuous",
          str(mf))
    for fn in ("init", "tick", "on_event", "health"):
        check(f"T1 四件套 {fn}", callable(getattr(wl, fn, None)))
    check("T1 去重器存在", callable(wl.MessageDeduplicator().is_duplicate))


# --------------------------------------------------------------------------
# T2/T3 入站 + 去重
# --------------------------------------------------------------------------
def t02_inbound_and_dedup():
    state = STATE
    owner_before = state.kv_get(STATE_OWNER_KEY) or ""
    tmpdir = tempfile.mkdtemp(prefix="wechat_t2_")
    tr = FakeTransport()
    organ = make_organ(tmpdir, state, tr)
    try:
        msg = text_msg("wxid_owner", "你好诺诺", message_id="m-in-1", context_token="tok-1")
        tr.updates = [{"ret": 0, "get_updates_buf": "buf-1", "msgs": [msg]}, empty_updates("buf-1")]
        r1 = asyncio.run(organ._poll_inbox_once())
        check("T2 入站处理计数", r1["handled"] == 1, str(r1))
        mms = master_signals(state, "你好诺诺")
        check("T2 master_message 进入State", len(mms) == 1, f"{len(mms)}条")
        if mms:
            d = payload_of(mms[0]).get("data") or {}
            check("T2 payload字段", (d.get("text") == "你好诺诺"
                                     and d.get("from_wechat") is True
                                     and d.get("sender") == "wxid_owner"), str(d))
            check("T2 route_to=dialogue", payload_of(mms[0]).get("route_to") == "dialogue")
        check("T2 context_token记录", organ._tokens.get("wxid_owner") == "tok-1",
              str(organ._tokens))
        claimed = state.kv_get(STATE_OWNER_KEY) or ""
        if owner_before:
            check("T2 已有主人不覆盖", claimed == owner_before, f"{owner_before}->{claimed}")
        else:
            check("T2 自动认主", claimed == "wxid_owner", f"owner={claimed}")
        check("T2 sync_buf持久化", os.path.exists(os.path.join(tmpdir, "sync.json")))

        # 同一 message_id 再来一次 → 去重
        tr.updates = [{"ret": 0, "get_updates_buf": "buf-1", "msgs": [msg]}]
        r2 = asyncio.run(organ._poll_inbox_once())
        mms2 = master_signals(state, "你好诺诺")
        check("T3 同message_id去重", r2["handled"] == 0 and len(mms2) == 1,
              f"handled={r2['handled']} count={len(mms2)}")

        # 不同 message_id 同文本 → 内容指纹去重
        msg3 = text_msg("wxid_owner", "你好诺诺", message_id="m-in-2", context_token="tok-1")
        tr.updates = [{"ret": 0, "get_updates_buf": "buf-1", "msgs": [msg3]}]
        r3 = asyncio.run(organ._poll_inbox_once())
        mms3 = master_signals(state, "你好诺诺")
        check("T3 同文本去重", r3["handled"] == 0 and len(mms3) == 1,
              f"handled={r3['handled']} count={len(mms3)}")
    finally:
        cleanup_state(state)
        organ = None


# --------------------------------------------------------------------------
# T4/T5/T6 出站
# --------------------------------------------------------------------------
def _find_send(posts):
    return [p for p in posts if p[0].endswith("/" + wl.EP_SEND_MESSAGE)]


def t04_outbox_reply_and_proactive():
    state = STATE
    tmpdir = tempfile.mkdtemp(prefix="wechat_t4_")
    tr = FakeTransport()
    organ = make_organ(tmpdir, state, tr)
    organ._owner = "wxid_owner"
    organ._tokens["wxid_owner"] = "tok-echo-9"
    try:
        state.kv_append(STATE_OUTBOX_KEY, {"text": "诺诺在呢", "to": "wxid_owner"})
        r = asyncio.run(organ._drain_outbox())
        check("T4 outbox排空", r["sent"] == 1 and r["failed"] == 0, str(r))
        sends = _find_send(tr.post_calls)
        check("T4 调用sendmessage", len(sends) == 1, f"{len(sends)}次")
        if sends:
            url, headers, payload = sends[-1]
            msg = payload["msg"]
            ok = (msg.get("to_user_id") == "wxid_owner"
                  and msg.get("message_type") == 2
                  and msg.get("item_list")[0]["type"] == 1
                  and msg.get("item_list")[0]["text_item"]["text"] == "诺诺在呢"
                  and msg.get("context_token") == "tok-echo-9")
            check("T4 消息体+context_token echo", ok, json.dumps(msg, ensure_ascii=False))
            check("T4 认证头", headers.get("Authorization") == "Bearer t-secret"
                  and headers.get("AuthorizationType") == "ilink_bot_token"
                  and headers.get("iLink-App-Id") == "bot", str(headers))
        check("T4 outbox已清空", state.kv_get(STATE_OUTBOX_KEY) in (None, []))

        # 主动说话: 无 to -> 主人(走 State wechat/owner)
        tr2 = FakeTransport()
        organ2 = make_organ(tmpdir, state, tr2)
        organ2._tokens["wxid_owner"] = "tok-echo-9"
        state.kv_set(STATE_OWNER_KEY, "wxid_owner")
        state.kv_append(STATE_OUTBOX_KEY, {"text": "主动汇报: 磁盘正常"})
        r2 = asyncio.run(organ2._drain_outbox())
        sends2 = _find_send(tr2.post_calls)
        ok2 = r2["sent"] == 1 and sends2 and sends2[0][2]["msg"]["to_user_id"] == "wxid_owner"
        check("T5 主动说话发给主人", ok2, str(r2))
    finally:
        cleanup_state(state)


def t06_session_expired_retry_without_token():
    state = STATE
    tmpdir = tempfile.mkdtemp(prefix="wechat_t6_")
    tr = FakeTransport()
    organ = make_organ(tmpdir, state, tr)
    organ._tokens["wxid_owner"] = "stale-token"
    state.kv_append(STATE_OUTBOX_KEY, {"text": "会话过期也要发出去", "to": "wxid_owner"})
    tr.send_error_queue = [{"ret": 0, "errcode": -14, "errmsg": "session expired"},
                           {"ret": 0, "errcode": 0}]
    try:
        r = asyncio.run(organ._drain_outbox())
        sends = _find_send(tr.post_calls)
        check("T6 -14后无token重试成功", r["sent"] == 1 and len(sends) == 2, f"sent={r['sent']}")
        if len(sends) == 2:
            first_ctx = sends[0][2]["msg"].get("context_token")
            second_ctx = sends[1][2]["msg"].get("context_token")
            check("T6 重试不带token", first_ctx == "stale-token" and second_ctx is None,
                  f"{first_ctx} -> {second_ctx}")
        check("T6 本地token已清", "wxid_owner" not in organ._tokens)
    finally:
        cleanup_state(state)


# --------------------------------------------------------------------------
# T7 登录
# --------------------------------------------------------------------------
def t07_qr_login():
    state = STATE
    tmpdir = tempfile.mkdtemp(prefix="wechat_t7_")
    tr = FakeTransport()
    tr.get_handlers = {
        wl.EP_GET_BOT_QR: {"qrcode": "hex-qr-1", "qrcode_img_content": "https://weixin.qq.com/x/1"},
        wl.EP_GET_QR_STATUS: {
            "status": "confirmed",
            "ilink_bot_id": "bot_scan_1",
            "bot_token": "tok-new",
            "base_url": "https://ilinkai.weixin.qq.com",
            "ilink_user_id": "wxid_nono",
        },
    }
    organ = WeChatLoopOrgan(client=state, data_dir=tmpdir, transport=tr)
    organ._qr_status_interval = 0.001
    try:
        acct = asyncio.run(organ._login_async(timeout_seconds=5))
        check("T7 登录返回凭据", acct.get("ilink_bot_id") == "bot_scan_1"
              and acct.get("bot_token") == "tok-new", str(acct))
        path = os.path.join(tmpdir, "account.json")
        check("T7 account.json 落盘", os.path.exists(path))
        if os.path.exists(path):
            saved = json.load(open(path, encoding="utf-8"))
            mode = os.stat(path).st_mode & 0o777
            check("T7 凭据内容+0600", saved.get("bot_token") == "tok-new"
                  and saved.get("ilink_user_id") == "wxid_nono" and mode == 0o600,
                  f"mode={oct(mode)}")
        qr_gets = [u for u, _ in tr.get_calls if wl.EP_GET_BOT_QR in u]
        check("T7 二维码请求", len(qr_gets) == 1)
        if wl.QRCODE_AVAILABLE:
            check("T7 qr.png 已保存", os.path.exists(os.path.join(tmpdir, wl.QR_FILE)))
        else:
            print("  (本机无qrcode库, 跳过PNG断言; 运行时环境建议用Hermes venv)")
    finally:
        cleanup_state(state)


def t08_continuous_run_smoke():
    """常驻主循环冒烟: inbox长轮询 + outbox轮询两任务并发跑, 真消费一条消息."""
    state = STATE
    tmpdir = tempfile.mkdtemp(prefix="wechat_t8_")
    tr = FakeTransport()
    organ = make_organ(tmpdir, state, tr)
    organ._owner = "wxid_owner"
    organ._tokens["wxid_owner"] = "tok-echo-9"
    organ._outbox_poll_seconds = 0.05
    state.kv_append(STATE_OUTBOX_KEY, {"text": "常驻循环冒烟消息", "to": "wxid_owner"})

    async def _drive():
        task = asyncio.create_task(organ.run_async())
        t0 = time.time()
        try:
            while time.time() - t0 < 3:
                outbox = state.kv_get(STATE_OUTBOX_KEY) or []
                if outbox == [] and len(_find_send(tr.post_calls)) >= 1:
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return time.time() - t0 < 3
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    try:
        ok = asyncio.run(_drive())
        sends = _find_send(tr.post_calls)
        check("T8 双循环并发跑通并消费outbox",
              ok and organ._started_at is not None and len(sends) == 1
              and (state.kv_get(STATE_OUTBOX_KEY) in (None, [])),
              f"sent={len(sends)} started={organ._started_at is not None}")
        if sends:
            m = sends[0][2]["msg"]
            check("T8 消息体正确", m["item_list"][0]["text_item"]["text"] == "常驻循环冒烟消息"
                  and m.get("context_token") == "tok-echo-9", json.dumps(m, ensure_ascii=False))
    finally:
        cleanup_state(state)


def main():
    cleanup_state(STATE)
    t01_spec()
    t02_inbound_and_dedup()
    t04_outbox_reply_and_proactive()
    t06_session_expired_retry_without_token()
    t07_qr_login()
    t08_continuous_run_smoke()
    cleanup_state(STATE)
    print(f"\n{'=' * 40}")
    p = sum(1 for _, ok in results if ok)
    print(f"结果: {p}/{len(results)} 通过")
    sys.exit(0 if p == len(results) else 1)


if __name__ == "__main__":
    main()
