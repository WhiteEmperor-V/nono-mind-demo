#!/usr/bin/env python3
"""outbox闸回归: 只发from_organ=dialogue的条目, 后台独白/无标记的一律拦
跑法: python3 tests/test_outbox_gate.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from organs.wechat_loop import _should_send_outbox_item

def test_gate():
    # 对话口标记 → 发
    assert _should_send_outbox_item({"text": "hi", "from_organ": "dialogue"}) is True
    # 无标记(当后台独白) → 拦
    assert _should_send_outbox_item({"text": "hi"}) is False
    # 标reflection(后台反思独白) → 拦
    assert _should_send_outbox_item({"text": "hi", "from_organ": "reflection"}) is False
    # 标coder后台日志 → 拦
    assert _should_send_outbox_item({"text": "hi", "from_organ": "coder"}) is False
    print("OK outbox闸(对话口发/后台独白拦)")

if __name__ == "__main__":
    test_gate()
    print("全部通过: outbox闸回归")
