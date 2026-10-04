#!/usr/bin/env python3
"""wellness 器官测试(宿主机跑, 无socket, MemState替身)

P0修复后行为(Codex审出4个P0, 已修):
  P0-1 体检查器官按unit类型分流: 常驻服务看is-active, reflection(timer型)看.timer
  P0-4 她不再自动改llm_client代码(那会把自己修坏+验证不可靠P0-2), 通道挂=降级兜底+报主人
  P0-3 git_safe_modify: 任一git步失败/改错=回滚+升级报主人, 绝不硬改装没事

覆盖:
  T1 模拟器官inactive(常驻) → wellness 自愈 restart 成功(静默, 不报主人)
  T1b reflection(timer型)挂着 → 不误判为"器官挂", 查的是 timer 在跑不在
  T3 脑子全挂 → heal_llm_chain 返回 'down'(不碰代码), 发 outbox 报病; 主人不回下轮不刷屏
  T4 CAPABILITIES 合法 + check_organs 按 unit 类型分流逻辑
运行: /usr/bin/python3 tests/test_wellness.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from organs import wellness


class MemState:
    """StateClient 兼容内存替身(够wellness用)."""
    def __init__(self):
        self.kv = {}
        self.sigs = {}
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

    def emit(self, *a, **k):
        pass

    def signals(self):
        return list(self.sigs.values())

    def drop(self, sig_id):
        self.sigs.pop(sig_id, None)
        return {"ok": True}


class WellnessTest(unittest.TestCase):
    def setUp(self):
        self.c = MemState()

    # ---------- T1: 常驻器官挂 → restart 自愈成功(静默) ----------
    def test_dead_organ_restart_heals_silently(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:2] == ["systemctl", "is-active"]:
                return types.SimpleNamespace(returncode=0, stdout="active\n", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(wellness, "check_llm_brain", return_value=(True, "ok", 0.1)), \
             mock.patch.object(wellness, "check_organs", return_value=["nono-canvas"]), \
             mock.patch.object(wellness, "check_poison_signals", return_value=0), \
             mock.patch.object(wellness, "check_log_errors", return_value=[]), \
             mock.patch.object(wellness.subprocess, "run", fake_run), \
             mock.patch.object(wellness.time, "sleep", lambda *a: None):
            report = wellness.run_one(self.c)

        self.assertEqual(report["organs_dead"], ["nono-canvas"])
        self.assertEqual(report["healed"], ["nono-canvas"])
        self.assertEqual(report["failed_organs"], [])
        self.assertIsNone(report["big_issue"])            # 小病静默, 不报主人
        self.assertIsNone(self.c.kv_get("wechat_outbox"))
        self.assertIn(["systemctl", "restart", "nono-canvas"], calls)
        self.assertEqual(self.c.kv_get("wellness/heal_log")["healed"], ["nono-canvas"])

    # ---------- T1b: reflection(timer型) — 查的是 timer 在跑不在, 不误判 ----------
    def test_reflection_timer_type_not_misjudged(self):
        # 模拟: reflection.service 是 inactive(timer 触发型, 平时就不该 active), 但 timer 在跑
        def fake_unit_active(unit):
            # 常驻服务 active; reflection.service(inactive)但 reflection.timer(active)
            if unit.endswith(".timer"):
                return True
            return unit != "nono-reflection"

        with mock.patch.object(wellness, "_unit_active", side_effect=fake_unit_active):
            dead = wellness.check_organs()
        # reflection timer 在跑 → 不算挂; 别的常驻都 active → 死的是空
        self.assertEqual(dead, [], "timer型reflection不该被误判为挂")

    # ---------- T3: 脑子全挂 → 'down'(不碰代码), 报病一次; 下轮不刷屏 ----------
    def test_brain_down_reports_once_then_retries_without_spam(self):
        # P0-4: heal_llm_chain 通道挂 = 返回 'down', 全程不 import/不改 llm_client
        with mock.patch.object(wellness, "check_llm_brain", return_value=(False, "down", None)), \
             mock.patch.object(wellness, "check_organs", return_value=[]), \
             mock.patch.object(wellness, "check_poison_signals", return_value=0), \
             mock.patch.object(wellness, "check_log_errors", return_value=[]):
            r1 = wellness.run_one(self.c)
            r2 = wellness.run_one(self.c)   # 主人不回, 下轮

        self.assertEqual(len(self.c.kv_get("wechat_outbox") or []), 1)   # 只报一次, 不刷屏
        self.assertIsNotNone(r1["big_issue"] and r2["big_issue"])
        # 她没碰代码 → 没打 baseline/rollback commit (P0-4 收窄白名单)
        self.assertIsNone(self.c.kv_get("wellness/heal_log"))

        # 4h后再病 → 节流窗过期, 允许再报一次(证明是窗口不是永久静音)
        self.c.kv["wellness/last_big_issue"]["at"] -= (4 * 3600 + 1)
        with mock.patch.object(wellness, "check_llm_brain", return_value=(False, "down", None)), \
             mock.patch.object(wellness, "check_organs", return_value=[]), \
             mock.patch.object(wellness, "check_poison_signals", return_value=0), \
             mock.patch.object(wellness, "check_log_errors", return_value=[]):
            wellness.run_one(self.c)
        self.assertEqual(len(self.c.kv_get("wechat_outbox")), 2)

    # ---------- T3b: heal_llm_chain 行为 = ok / down, 绝不 'reordered'(不碰代码) ----------
    def test_heal_llm_chain_never_touches_code(self):
        # 脑子通
        with mock.patch.object(wellness, "check_llm_brain", return_value=(True, "reply_ok", 0.1)):
            self.assertEqual(wellness.heal_llm_chain(self.c), "ok")
        # 脑子挂 → 降级兜底, 返回 down, 没有文件写入动作
        with mock.patch.object(wellness, "check_llm_brain", return_value=(False, "down", None)):
            self.assertEqual(wellness.heal_llm_chain(self.c), "down")

    # ---------- T4: CAPABILITIES 合法 + unit 类型分流常量正确 ----------
    def test_capabilities_valid_and_service_split(self):
        from immune import capabilities as caps
        for cap in wellness.CAPABILITIES:
            ok, err = caps.validate_capability(cap)
            self.assertTrue(ok, err)
        # 分流正确: 常驻4个 + timer1个, 无重叠
        self.assertEqual(set(wellness.ALWAYS_SERVICES) | set(wellness.TIMER_SERVICES),
                         {"nono-state", "nono-core", "nono-canvas", "nono-wechat", "nono-reflection"})
        self.assertFalse(set(wellness.ALWAYS_SERVICES) & set(wellness.TIMER_SERVICES))



class SuppressedIssueTest(unittest.TestCase):
    """P1-1: 4h节流压下的待发大事, 病自愈后到期不该再发过时话."""
    def setUp(self):
        self.c = MemState()

    def _run(self, brain_ok, dead, brain_ch="down"):
        with mock.patch.object(wellness, "check_llm_brain", return_value=(brain_ok, brain_ch, None)), \
             mock.patch.object(wellness, "check_organs", return_value=dead), \
             mock.patch.object(wellness, "check_poison_signals", return_value=0), \
             mock.patch.object(wellness, "check_log_errors", return_value=[]):
            return wellness.run_one(self.c)

    def test_suppressed_issue_dropped_after_heal_no_stale_report(self):
        self._run(False, [])                                  # 报病一次
        self.assertEqual(len(self.c.kv_get("wechat_outbox") or []), 1)
        self._run(False, [])                                  # 4h内 → 压下, 记 suppressed
        sup = self.c.kv_get("wellness/suppressed_issue")
        self.assertIsNotNone(sup, "被节流的大事该记下待发")
        self.assertEqual(sup["key"], "brain")

        # 模拟"窗口到期": 上次发送时间推回4h前, 但病本轮已好(brain通)
        self.c.kv["wellness/last_big_issue"]["at"] -= (wellness.REPORT_COOLDOWN + 1)
        self._run(True, [])
        self.assertEqual(len(self.c.kv_get("wechat_outbox")), 1, "病好了不该补发过时话")
        self.assertIsNone(self.c.kv_get("wellness/suppressed_issue"), "病好了该撤掉待发")

    def test_suppressed_issue_kept_and_sent_if_still_sick(self):
        self._run(False, [])                                  # 报病一次
        self._run(False, [])                                  # 压下
        self.assertIsNotNone(self.c.kv_get("wellness/suppressed_issue"))
        # 窗口到期 且 病仍在 → 正常再报(实况), 并清掉待发
        self.c.kv["wellness/last_big_issue"]["at"] -= (wellness.REPORT_COOLDOWN + 1)
        self._run(False, [])
        self.assertEqual(len(self.c.kv_get("wechat_outbox")), 2)
        self.assertIsNone(self.c.kv_get("wellness/suppressed_issue"))


class PoisonSignalTest(unittest.TestCase):
    """P1-5: 毒信号不只认 loop_error, 滞留未衰减的反复失败形态也要清."""
    def setUp(self):
        self.c = MemState()

    def test_poison_clears_loop_error_and_stale_other(self):
        now = 1_000_000.0
        stale = wellness.POISON_STALE_SEC + 3600
        self.c.sigs = {
            "loop_hot":  {"sig_id": "loop_hot", "sig_type": "loop_error", "strength": 0.8, "created": now},
            "msg_old":   {"sig_id": "msg_old", "sig_type": "master_message", "strength": 0.9, "created": now - stale},
            "msg_fresh": {"sig_id": "msg_fresh", "sig_type": "master_message", "strength": 0.9, "created": now},
            "loop_weak": {"sig_id": "loop_weak", "sig_type": "loop_error", "strength": 0.3, "created": now - stale},
        }
        buf = io.StringIO()
        with mock.patch.object(wellness.time, "time", return_value=now), \
             contextlib.redirect_stdout(buf):
            n = wellness.check_poison_signals(self.c)
        self.assertEqual(n, 2)                                  # loop_hot + msg_old
        self.assertNotIn("loop_hot", self.c.sigs)
        self.assertNotIn("msg_old", self.c.sigs)
        self.assertIn("msg_fresh", self.c.sigs, "新鲜信号不是毒, 不能误清")
        self.assertIn("loop_weak", self.c.sigs, "强度未达阈值不清")
        self.assertIn("msg_old", buf.getvalue(), "清前要 print 留痕")


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


class GitBaselineTest(unittest.TestCase):
    """P1-2: git_baseline 固化基线不许把别人已staged的别的文件卷进来."""
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="wellness_git_")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "tester")
        self._write("ours.txt", "v1\n")
        self._write("theirs.txt", "v1\n")
        _git(self.repo, "add", "ours.txt", "theirs.txt")
        _git(self.repo, "commit", "-q", "-m", "init")

    def _write(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def test_baseline_does_not_swallow_others_staged_files(self):
        self._write("theirs.txt", "theirs-dirty\n")
        _git(self.repo, "add", "theirs.txt")          # 别人staged了
        self._write("ours.txt", "ours-dirty\n")       # 我们的也脏
        with mock.patch.object(wellness, "ROOT", self.repo):
            self.assertTrue(wellness.git_baseline(["ours.txt"], "p1-2"))
        # 固化commit(HEAD~1)只含 ours.txt, 不含别人的
        fixed = _git(self.repo, "show", "--pretty=format:", "--name-only", "HEAD~1").stdout
        self.assertIn("ours.txt", fixed)
        self.assertNotIn("theirs.txt", fixed)
        # 别人的staged改动没被吞, 仍在暂存区
        staged = _git(self.repo, "diff", "--cached", "--name-only").stdout
        self.assertIn("theirs.txt", staged)

    def test_baseline_skips_fixup_when_our_files_clean(self):
        self._write("theirs.txt", "theirs-dirty\n")
        _git(self.repo, "add", "theirs.txt")          # 脏的是别处, 我们的文件干净
        with mock.patch.object(wellness, "ROOT", self.repo):
            self.assertTrue(wellness.git_baseline(["ours.txt"], "p1-2"))
        # 没有"固化改动前基线"commit, 只有 init + baseline
        log = _git(self.repo, "log", "--format=%s").stdout
        self.assertNotIn("固化改动前基线", log)
        self.assertEqual(_git(self.repo, "rev-list", "--count", "HEAD").stdout.strip(), "2")

if __name__ == "__main__":
    unittest.main(verbosity=2)
