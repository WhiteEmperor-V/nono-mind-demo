#!/usr/bin/env python3
"""消息小loop(main.py): 接微信聊天频道直接回话, 不绕core主循环.

架构(9/29主人拍板):
  wechat_loop收消息 -> route master_message -> 【本loop】装dialogue能力直接回话 -> outbox发
  脑子不变(全走agnes), 后台干活仍走大loop编排, 只把消息收发这条线归本loop.

四件套: init/tick/on_event/health
on_event: 收到master_message信号 -> dialogue.handle生成回复 -> kv_append wechat_outbox(带from_organ=dialogue, 过wechat_loop的outbox闸)
"""
import os, sys, json, time

# 让 main.py 无论从哪起都能 import 到 nono-mind 根目录的模块
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from organs import dialogue  # 对话能力(人设/上下文/记忆/MEMO 全在这)

_OUTBOX = "wechat_outbox"
_HEALTH = "organs/health/message_loop"


def _log(msg):
    print(f"[message_loop] {msg}", flush=True)


def init(state=None):
    """注册能力: 本loop提供 chat_reply(接master_message回话)"""
    if state is not None:
        state["capabilities"] = ["chat_reply"]
        state["last_result"] = None
    _log("消息小loop已初始化(装dialogue对话能力)")
    return True


def tick(state=None):
    """event型无周期动作, 单轮健康自检(供免疫层健康检查)"""
    try:
        import importlib
        m = importlib.import_module("organs.dialogue")
        ok = hasattr(m, "handle")
    except Exception as e:
        ok = False
    _log(f"tick: dialogue对话能力可用={ok}")
    return {"ok": ok, "at": time.time()}


def on_event(event=None):
    """收到master_message信号 -> dialogue回话 -> 带标记投outbox(过wechat闸).
    单个信号失败要catch住(转错误日志), 别杀整个loop."""
    try:
        _handle_one(event)
    except Exception as e:
        _log(f"on_event 处理失败(catch住不崩loop): {e}")
        try:
            from state.server import StateClient
            c = StateClient()
            c.kv_append("loop_error", {"at": time.time(), "organ": "message_loop",
                                       "err": str(e), "event": str(event)[:100]})
            c.close()
        except Exception:
            pass


def _handle_one(event):
    from state.server import StateClient
    from immune import capabilities as caps

    # event 可能是 {sig_type, payload} 或直接 payload
    sig_type = event.get("sig_type", "master_message") if isinstance(event, dict) else "master_message"
    if sig_type != "master_message":
        _log(f"非master_message信号({sig_type}), 本loop不管")
        return

    payload = event.get("payload") if isinstance(event, dict) else event
    if isinstance(payload, str):
        payload = json.loads(payload)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict) or not data.get("from_wechat"):
        _log("非微信来的信号, 本loop不管(其它来源走core)")
        return

    text = str(data.get("text") or "").strip()
    sender = str(data.get("sender") or "owner").strip()
    if not text:
        return

    # 调 dialogue 回话(它自己会 _remember 记历史 + 提 MEMO, 本loop不重复记)
    c = StateClient()
    try:
        reply = dialogue.handle({"data": data}, c)
        if not reply:
            _log("dialogue回话为空, 不投outbox")
            return
        # 带 from_organ=dialogue 标记 -> wechat_loop 的 outbox 闸放行
        c.kv_append(_OUTBOX, {"to": sender, "text": reply, "from_organ": "dialogue"})
        c.kv_set(_HEALTH, {"ok": True, "at": time.time(), "organ": "message_loop"})
        _log(f"回话已投outbox -> {sender[:12]}: {reply[:50]}")
    finally:
        c.close()


def health():
    """健康: dialogue模块能import = 健康"""
    try:
        from organs import dialogue  # noqa
        return hasattr(dialogue, "handle")
    except Exception:
        return False


if __name__ == "__main__":
    # 真跑一遍: 模拟一条微信master_message信号, 证明回话能生成+能投outbox
    import os
    from state.server import StateClient
    c = StateClient()
    try:
        init()
        _log("tick自检:")
        print(tick())
        _log(f"health: {health()}")
        # 模拟一条微信消息(不走真实iLink, 只验on_event->dialogue->outbox这条线)
        fake = {"sig_type": "master_message",
                "payload": {"data": {"text": "消息小loop测试: 回我一句你好",
                                     "from_wechat": True, "sender": "owner",
                                     "chat_type": "dm"}}}
        _log("模拟master_message信号进on_event:")
        on_event(fake)
        time.sleep(1)
        box = c.kv_get(_OUTBOX) or []
        _log(f"outbox现有 {len(box)} 条, 最后一条: {str(box[-1])[:120] if box else '(空)'}")
    finally:
        c.close()
