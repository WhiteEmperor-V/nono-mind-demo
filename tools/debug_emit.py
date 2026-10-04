#!/usr/bin/env python3
"""诺诺调试新身体的专用入口(2026-10-02主人定: 测试别刷屏主人微信).

用法:  python3 tools/debug_emit.py "给新身体的一句测试话"

规则(防再次打扰主人):
- 一律塞 wechat_outbox 时带 debug=True + from_organ=debug → outbox闸拦掉, 不发主人微信
- 塞 master_message 时 sender 留空(不当主人) → 走她内部loop, 她回话也进debug桶不回你
以后诺诺调试 hermes, 全走这个, 不再拿主人微信ID当发话人."""
import sys, json
from state.server import StateClient

def emit(text: str, as_task=False):
    c = StateClient()
    owner = c.kv_get("wechat/owner") or ""
    # 测试消息: 标debug, outbox闸(wechat_loop._should_send_outbox_item)看到debug一律丢弃, 不发主人
    c.kv_append("wechat_outbox", {"to": owner, "text": text,
                                   "from_organ": "debug", "debug": True})
    # 同时塞一条 master_message 让她内部接住(回话也进debug桶, 不回主人微信)
    if as_task:
        c.emit("master_message", {"data": {"text": text, "from_wechat": True,
                                            "debug": True}, "debug": True},
               decay=0.01, source="debug")
    print(f"已塞测试(标debug, 不发主人微信): {text[:50]}")
    c.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 tools/debug_emit.py '测试话' [--task]")
        sys.exit(1)
    emit(sys.argv[1], as_task="--task" in sys.argv)
