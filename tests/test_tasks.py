import json, os, sys, time, unittest
sys.path.insert(0, "/root/nono-mind")

class TestTaskLifecycle(unittest.TestCase):
    def setUp(self):
        from state.server import StateClient
        self.c = StateClient()
        self.tid = f"test_{self.id().split(chr(46))[-1]}_{int(time.time()*1000)}"

    def test_full_lifecycle(self):
        r = self.c.task_create(self.tid, intent="code_task", assigned_to="coder",
                               params={"demand": "测试任务"})
        self.assertTrue(r["ok"])
        t = self.c.task_get(self.tid)
        self.assertEqual(t["status"], "queued")
        r = self.c.task_claim(self.tid, owner_pid=12345)
        self.assertTrue(r["ok"])
        t = self.c.task_get(self.tid)
        self.assertEqual(t["status"], "running")
        self.assertEqual(t["attempt"], 1)
        r = self.c.task_finish(self.tid, "done", {"out": "完成"})
        self.assertTrue(r["ok"])
        t = self.c.task_get(self.tid)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["result"]["out"], "完成")
        self.assertIsNotNone(t["finished"])

    def test_illegal_transitions(self):
        self.c.task_create(self.tid)
        # queued→finish 是非法的(必须先claim)
        with self.assertRaises(RuntimeError) as cm:
            self.c.task_finish(self.tid, "done")
        self.assertIn("非法转移", str(cm.exception))
        # double claim非法
        self.c.task_claim(self.tid)
        with self.assertRaises(RuntimeError):
            self.c.task_claim(self.tid)

    def test_cancel_cooperative(self):
        self.c.task_create(self.tid)
        self.c.task_claim(self.tid)
        r = self.c.task_cancel(self.tid)
        self.assertTrue(r["ok"])
        t = self.c.task_get(self.tid)
        self.assertEqual(t["status"], "cancelling")
        # cancelled后不能再claim
        with self.assertRaises(RuntimeError):
            self.c.task_claim(self.tid)

    def test_requeue_with_backoff(self):
        self.c.task_create(self.tid)
        self.c.task_claim(self.tid)
        r = self.c.task_requeue(self.tid, attempt=2)
        self.assertTrue(r["ok"])
        t = self.c.task_get(self.tid)
        self.assertEqual(t["status"], "queued")
        self.assertEqual(t["attempt"], 2)
        # 重试后再claim
        self.c.task_claim(self.tid)
        t = self.c.task_get(self.tid)
        self.assertEqual(t["attempt"], 3)

    def test_create_dup_idempotent_fail(self):
        self.c.task_create(self.tid, intent="a")
        # 再次create同id: sub=create但task已存在 → 走到"非法转移"分支(create不在legal里)
        with self.assertRaises(RuntimeError) as cm:
            self.c.task_create(self.tid, intent="b")
        self.assertIn("task已存在", str(cm.exception))

if __name__ == "__main__":
    unittest.main(verbosity=2)
