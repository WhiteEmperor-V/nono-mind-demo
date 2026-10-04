#!/usr/bin/env python3
"""消息小loop回归: 验 微信chat->message_loop->dialogue->outbox 这条线不绕core
跑法: python3 tests/test_message_loop.py
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from state.server import StateClient


class TestMessageLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = StateClient()
        cls.c.kv_set("wechat_outbox", [])
        cls._mark_test = "MESSAGE_LOOP_TEST"

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def test_manifest_declares_chat_reply(self):
        import yaml
        mf = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "plugins", "message_loop", "manifest.yaml")))
        self.assertIn("chat_reply", mf.get("provides", []))
        self.assertIn("master_message", mf.get("subscribes", []))

    def test_on_event_replies_to_wechat(self):
        from plugins.message_loop import main as mloop
        fake = {"sig_type": "master_message",
                "payload": {"data": {"text": "消息小loop测试: 回我一句你好",
                                     "from_wechat": True, "sender": "owner",
                                     "chat_type": "dm"}}}
        mloop.on_event(fake)
        time.sleep(0.5)
        box = self.c.kv_get("wechat_outbox") or []
        found = [i for i in box if i.get("from_organ") == "dialogue" and "owner" == i.get("to")]
        self.assertTrue(found, "outbox 里没找到 message_loop 发的那条对话口回复")

    def test_non_wechat_not_handled(self):
        from plugins.message_loop import main as mloop
        mloop.on_event({"sig_type": "master_message",
                        "payload": {"data": {"text": "非微信来源", "from_wechat": False}}})
        time.sleep(0.2)
        box = self.c.kv_get("wechat_outbox") or []
        self.assertNotIn(self._mark_test, str(box[-1:]) if box else "", "非微信来源不应被message_loop处理")


if __name__ == "__main__":
    unittest.main(verbosity=2)
