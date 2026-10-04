#!/usr/bin/env python3
"""分层架构测试: 能力注册表/大loop匹配/任务分发 (MemState, 无socket)"""
import json, os, sys, unittest

sys.path.insert(0, "/root/nono-mind")

class MemState:
    """内存版State(与tests/test_p1_fixes.py同款)"""
    def __init__(self):
        self.kv = {}
        self.rev = 0
    def kv_get(self, key, default=None):
        return self.kv.get(key, default)
    def kv_set(self, key, val):
        self.rev += 1
        self.kv[key] = val
        return {"ok": True}
    def kv_del(self, key):
        self.kv.pop(key, None)
        return {"ok": True}
    def kv_list(self, prefix):
        return {k: v for k, v in self.kv.items() if k.startswith(prefix)}
    def kv_append(self, key, item):
        arr = self.kv.get(key)
        if not isinstance(arr, list):
            arr = []
        arr.append(item)
        self.kv[key] = arr
        return {"ok": True}
    def get_history_range(self, days=1):
        return self.kv.get("dialogue_history/2026-09-10", []) if isinstance(self.kv.get("dialogue_history/2026-09-10"), list) else []
    def emit(self, *a, **k): pass
    def drop(self, *a, **k): pass

class TestCapabilitiesRegistry(unittest.TestCase):
    def setUp(self):
        from immune import capabilities as caps
        self.caps = caps
        self.c = MemState()

    def test_register_valid(self):
        ok, err = self.caps.register(self.c, {
            "organ": "canvas", "name": "canvas_create",
            "description": "对画布进行创作: 生成HTML页面并推送。何时用: 主人要求画东西。何时不用: 查数据。",
            "params_schema": {"demand": "string"},
            "examples": ["画一个鹈鹕"]})
        self.assertTrue(ok, err)
        stored = self.c.kv_get("capabilities/canvas")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["name"], "canvas_create")

    def test_reject_short_description(self):
        ok, err = self.caps.register(self.c, {"organ": "x", "name": "y", "description": "太短"})
        self.assertFalse(ok)
        self.assertIn("过短", err)

    def test_reject_no_boundary(self):
        ok, err = self.caps.register(self.c, {
            "organ": "x", "name": "y",
            "description": "这是一个没有任何边界说明的长描述, 超过二十个字符了"})
        self.assertFalse(ok)
        self.assertIn("何时不用", err)

    def test_unregister(self):
        self.caps.register(self.c, {"organ": "a", "name": "n1",
            "description": "测试用能力声明, 含边界: 何时不用——无。够长了吧这个描述有二十多字了",
            })
        ok = self.caps.unregister(self.c, "a", "n1")
        self.assertTrue(ok)
        self.assertEqual(self.c.kv_get("capabilities/a"), None)

    def test_list_all_and_prompt(self):
        self.caps.register(self.c, {"organ": "canvas", "name": "c1",
            "description": "画布创作能力: 生成页面推送。何时用: 要画东西。何时不用: 查数据时不用这个。",
            "examples": ["画个猫"]})
        self.caps.register(self.c, {"organ": "dialogue", "name": "chat",
            "description": "日常对话能力: 聊天回答问题。何时用: 聊天。何时不用: 干活时不用这个。",
            })
        allc = self.caps.list_all(self.c)
        self.assertEqual(len(allc), 2)
        prompt = self.caps.to_prompt(self.c)
        self.assertIn("canvas/c1", prompt)
        self.assertIn("dialogue/chat", prompt)

class TestLLMMatch(unittest.TestCase):
    def setUp(self):
        from core import loop as core_loop
        self.cl = core_loop
        self.c = MemState()

    def test_extract_text(self):
        p = {"data": {"text": "画一个鹈鹕", "from_wechat": True}}
        self.assertEqual(self.cl._extract_text(p), "画一个鹈鹕")
        self.assertEqual(self.cl._extract_text(json.dumps(p)), "画一个鹈鹕")

    def test_match_output_parsing(self):
        import core.loop as cl
        # 模拟LLM返回(打桩chat)
        class FakeLLM:
            def __init__(self, resp): self.resp = resp
            def chat(self, msgs, max_tokens=1000, _retry=0): return self.resp
        raw = '{"organ": "canvas", "params": {"demand": "画一个鹈鹕"}, "reason": "画布创作"}'
        import llm_client
        orig = llm_client.chat
        llm_client.chat = FakeLLM(raw).chat
        try:
            r = cl._llm_match(self.c, "画一个鹈鹕")
        finally:
            llm_client.chat = orig
        self.assertEqual(r["organ"], "canvas")
        self.assertEqual(r["params"]["demand"], "画一个鹈鹕")

    def test_no_match_parsing(self):
        import core.loop as cl
        class FakeLLM:
            def chat(self, msgs, max_tokens=1000, _retry=0):
                return '{"organ": null, "reason": "没有对应能力"}'
        import llm_client
        orig = llm_client.chat
        llm_client.chat = FakeLLM().chat
        try:
            r = cl._llm_match(self.c, "帮我订外卖")
        finally:
            llm_client.chat = orig
        self.assertIsNone(r["organ"])

    def test_organ_validation(self):
        import core.loop as cl
        # organ不在注册表(幻觉) → modeling/mixed分支返回None organ(交NO_MATCH派coder)
        import llm_client
        class FakeLLM:
            def chat(self, msgs, max_tokens=1000, _retry=0):
                return '{"organ": "ghost_organ", "params": {}, "reason": "幻觉"}'
        orig = llm_client.chat
        llm_client.chat = FakeLLM().chat
        try:
            # 用建模任务词触发modeling分支(普通对话走透传dialogue, 不触发幻觉兜底)
            r = cl.capability_route(self.c, "master_message", {"data": {"text": "帮我建模一个3D贪吃蛇游戏"}})
        finally:
            llm_client.chat = orig
        self.assertIsNone(r["organ"])

class TestCanvasOrgan(unittest.TestCase):
    def setUp(self):
        self.c = MemState()

    def test_capabilities_declared(self):
        from organs.canvas_organ import CAPABILITIES
        self.assertTrue(len(CAPABILITIES) >= 1)
        self.assertEqual(CAPABILITIES[0]["organ"], "canvas")

    def test_push_failure_honest(self):
        # 推送失败时应诚实记录(pushed=False)
        from organs import canvas_organ
        import llm_client
        class FakeLLM:
            def chat(self, msgs, max_tokens=1000, _retry=0): return ""
        # 直接测_push_canvas的错误路径(错误URL)
        orig = canvas_organ.CANVAS_PUSH_URL
        canvas_organ.CANVAS_PUSH_URL = "http://127.0.0.1:1/push"
        try:
            ok, err = canvas_organ._push_canvas("t", "t", "/tmp/nonexist.html")
        finally:
            canvas_organ.CANVAS_PUSH_URL = orig
        self.assertFalse(ok)
        self.assertTrue(err)

if __name__ == "__main__":
    unittest.main(verbosity=2)
