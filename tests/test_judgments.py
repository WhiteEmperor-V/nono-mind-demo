#!/usr/bin/env python3
"""judgments 判断账测试(宿主机跑, 无socket, MemState替身, 不打真网络).

覆盖任务书四用例:
  T1 写一条judgment → 读回
  T2 探测函数表调用正确: 条件成立→void, 不成立→保持active(scnet恢复/没恢复两种)
  T3 换脑触发重验: 脑子名变→问新脑子, 不成立/拿不准→recheck
  T4 没有探测函数的判断(纯主观) → 跳过, 不误标status
另加两挂点烟雾测: loop.note_decision(挂点A写) / llm_client._track_brain(挂点C检测).
运行: /usr/bin/python3 tests/test_judgments.py
"""
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from organs import judgments


class MemState:
    """StateClient 兼容内存替身(够judgments用)."""
    def __init__(self):
        self.kv = {}
        self.rev = 0

    def kv_set(self, key, value, expected_revision=None):
        self.rev += 1
        self.kv[key] = json.loads(json.dumps(value))
        return {"ok": True, "revision": self.rev}

    def kv_get(self, key):
        v = self.kv.get(key)
        return json.loads(json.dumps(v)) if v is not None else None

    def kv_append(self, key, item):
        self.kv.setdefault(key, []).append(json.loads(json.dumps(item)))
        self.rev += 1
        return {"ok": True, "revision": self.rev}

    def kv_del(self, key):
        self.kv.pop(key, None)
        return {"ok": True}

    def kv_list(self, prefix=""):
        return {k: v for k, v in self.kv.items() if k.startswith(prefix)}


class JudgmentsTest(unittest.TestCase):
    def setUp(self):
        self.c = MemState()

    # ---------- T1: 写一条judgment → 读回 ----------
    def test_write_then_read(self):
        jid = judgments.write_judgment(
            self.c, "scnet挂了, 走降级通道", why="主脑scnet 402/超时",
            invalid_if="scnet通道恢复", probe="probe_scnet_alive", brain="Qwen3.6-Flash")
        rec = self.c.kv_get(f"judgments/{jid}")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["text"], "scnet挂了, 走降级通道")
        self.assertEqual(rec["invalid_if"], "scnet通道恢复")
        self.assertEqual(rec["probe"], "probe_scnet_alive")
        self.assertEqual(rec["brain"], "Qwen3.6-Flash")
        self.assertEqual(rec["status"], "active")
        self.assertIn("created", rec)
        self.assertIn("verified_at", rec)
        # 同正文重复写=覆盖同一条(防judgments/爆炸)
        jid2 = judgments.write_judgment(self.c, "scnet挂了, 走降级通道", why="再确认一次")
        self.assertEqual(jid, jid2)
        self.assertEqual(len(self.c.kv_list("judgments/")), 1)

    # ---------- T2: 探测函数表调用正确(标void / 不标) ----------
    def test_probe_table_void_or_keep(self):
        void_jid = judgments.write_judgment(self.c, "判断甲", invalid_if="条件甲", probe="probe_always_true")
        keep_jid = judgments.write_judgment(self.c, "判断乙", invalid_if="条件乙", probe="probe_always_false")
        with mock.patch.dict(judgments.PROBES, {
                "probe_always_true": lambda j: True,
                "probe_always_false": lambda j: False}):
            res = judgments.probe_all(self.c)
        self.assertEqual(res["voided"], 1)
        self.assertEqual(res["checked"], 2)
        self.assertEqual(self.c.kv_get(f"judgments/{void_jid}")["status"], "void")   # 条件成立→作废
        self.assertEqual(self.c.kv_get(f"judgments/{keep_jid}")["status"], "active")  # 条件不成立→保持

    def test_scnet_probe_recovered_vs_down(self):
        """模拟scnet恢复/没恢复两种, 看status变化对不对."""
        jid = judgments.write_judgment(
            self.c, "scnet挂了, 走降级通道", invalid_if="scnet通道恢复", probe="probe_scnet_alive")
        # ① scnet恢复(探测成功返回) → void
        with mock.patch("llm_client._try_chain", return_value=("OK", "Qwen3.6-Flash")):
            judgments.probe_all(self.c)
        self.assertEqual(self.c.kv_get(f"judgments/{jid}")["status"], "void")
        # ② 新写一条同款(scnet又挂) → 探测失败(scnet没通) → 保持active
        judgments.write_judgment(
            self.c, "scnet挂了, 走降级通道", invalid_if="scnet通道恢复", probe="probe_scnet_alive")
        with mock.patch("llm_client._try_chain", side_effect=RuntimeError("All channels failed")):
            judgments.probe_all(self.c)
        self.assertEqual(self.c.kv_get(f"judgments/{jid}")["status"], "active")

    # ---------- T3: 换脑触发重验 → recheck ----------
    def test_brain_change_triggers_recheck(self):
        jid = judgments.write_judgment(self.c, "缓存能省一次LLM调用", why="当时实测省了")
        # 首次记录脑子名 → 不算换脑, 不触发
        self.assertFalse(judgments.note_brain(self.c, "Qwen3.6-Flash"))
        self.assertEqual(self.c.kv_get(f"judgments/{jid}")["status"], "active")
        # 换脑(Qwen3.6-Flash → nemotron-550b) → 问新脑子; 回"不成立" → recheck
        self.assertTrue(judgments.note_brain(self.c, "nemotron-550b", ask=lambda j, b: "不成立"))
        self.assertEqual(self.c.kv_get(f"judgments/{jid}")["status"], "recheck")
        self.assertEqual(self.c.kv_get("llm/current_brain")["brain"], "nemotron-550b")
        reviews = self.c.kv_get("judgment_reviews")
        self.assertEqual(reviews[-1]["brain"], "nemotron-550b")
        self.assertEqual(reviews[-1]["verdict"], "不成立")

    def test_brain_change_keeps_active_when_affirmed(self):
        jid = judgments.write_judgment(self.c, "判断丙", why="依据丙")
        judgments.note_brain(self.c, "brainA")
        judgments.note_brain(self.c, "brainB", ask=lambda j, b: "成立")
        self.assertEqual(self.c.kv_get(f"judgments/{jid}")["status"], "active")   # 成立→保持
        judgments.note_brain(self.c, "brainC", ask=lambda j, b: "拿不准")
        self.assertEqual(self.c.kv_get(f"judgments/{jid}")["status"], "recheck")  # 拿不准→recheck

    # ---------- T4: 没有探测函数的判断跳过, 不误标 ----------
    def test_no_probe_judgment_skipped(self):
        a = judgments.write_judgment(self.c, "纯主观判断, 没有探测函数", invalid_if="我改主意了")
        b = judgments.write_judgment(self.c, "配了不存在的探测函数", invalid_if="条件", probe="probe_not_registered")
        res = judgments.probe_all(self.c)
        self.assertEqual(res["skipped"], 2)
        self.assertEqual(res["checked"], 0)
        self.assertEqual(res["voided"], 0)
        self.assertEqual(self.c.kv_get(f"judgments/{a}")["status"], "active")   # 不瞎标
        self.assertEqual(self.c.kv_get(f"judgments/{b}")["status"], "active")

    # ---------- 每日探测节流 ----------
    def test_daily_probe_throttled(self):
        judgments.write_judgment(self.c, "判断丁", invalid_if="x", probe="probe_always_true")
        with mock.patch.dict(judgments.PROBES, {"probe_always_true": lambda j: True}):
            r1 = judgments.maybe_daily_probe(self.c, today="2026-09-27")
            r2 = judgments.maybe_daily_probe(self.c, today="2026-09-27")
        self.assertIsNotNone(r1)
        self.assertIsNone(r2)   # 同一天第二次 → 跳过(省资源)

    # ---------- 挂点A: loop.note_decision 真写入 ----------
    def test_hook_a_loop_note_decision(self):
        from core import loop
        loop.note_decision(self.c, "回退旧固定路由", why="匹配层挂", invalid_if="scnet通道恢复",
                           probe="probe_scnet_alive")
        items = list(self.c.kv_list("judgments/").values())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["probe"], "probe_scnet_alive")
        self.assertEqual(items[0]["status"], "active")

    # ---------- 挂点C: llm_client._track_brain 检测变化并触发 ----------
    def test_hook_c_track_brain_detects_change(self):
        import llm_client
        calls = []
        dummy = mock.Mock()
        llm_client._brain_seen = None
        with mock.patch("state.server.StateClient", return_value=dummy), \
             mock.patch("organs.judgments.note_brain", side_effect=lambda c, used, ask=None: calls.append(used)):
            changed = llm_client._track_brain("Qwen3.6-Flash")   # 首次 → 记下
            same = llm_client._track_brain("Qwen3.6-Flash")       # 没变 → 不碰State
            switched = llm_client._track_brain("nemotron-550b")   # 换脑 → 触发
        self.assertEqual(calls, ["Qwen3.6-Flash", "nemotron-550b"])
        self.assertFalse(same)
        self.assertTrue(switched)


if __name__ == "__main__":
    unittest.main(verbosity=2)
