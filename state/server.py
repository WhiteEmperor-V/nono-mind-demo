#!/usr/bin/env python3
"""State服务进程: 唯一写者(修复多进程写锁竞争)
器官(任意进程)通过Unix socket连接; 写请求排队由本进程串行执行; 读走各自WAL只读连接
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import socket, sqlite3, json, time, os, threading, uuid, struct
from state.model import Signal, PRIORITY, P0_TTL, STRENGTH_MAX, DECAY_MIN
from dataclasses import dataclass, field

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "state.db")
SOCK_PATH = "/tmp/nono_state.sock"
MAX_MSG = 1024 * 1024   # 协议v2: 单消息上限1MB, 超限直接拒绝

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (sig_id TEXT PRIMARY KEY, sig_type TEXT NOT NULL,
    payload TEXT, strength REAL DEFAULT 1.0, decay REAL DEFAULT 0.3, created REAL, source TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, revision INTEGER);
CREATE INDEX IF NOT EXISTS idx_strength ON signals(strength DESC);
CREATE INDEX IF NOT EXISTS idx_type_created ON signals(sig_type, created);
CREATE TABLE IF NOT EXISTS events (event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, etype TEXT NOT NULL, source TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(etype, ts);
"""

class StateServer:
    """唯一写者. 所有写请求经socket进来排队串行执行."""
    def __init__(self, db_path=DB_PATH, threshold=0.25, tick=0.5, autobind=True):
        self.threshold, self.tick = threshold, tick
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA); self.conn.commit()
        self._wlock = threading.Lock()
        self._last_checkpoint = time.time()
        self._ops_since_cp = 0
        self.CP_INTERVAL = 30      # 秒
        self.CP_OPS = 50           # 操作数
        self.CP_KEEP = 5           # 保留快照数
        self._rev = int(self.conn.execute("SELECT COALESCE(MAX(revision),0) FROM kv").fetchone()[0])
        self._migrate_history()    # P2-4: 旧dialogue_history单体key一次性迁移为按天分片
        if not autobind:
            return
        if os.path.exists(SOCK_PATH): os.unlink(SOCK_PATH)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        self.sock.listen(32)
        self.alive = True

    def _migrate_history(self):
        """一次性迁移: dialogue_history(单体数组) → dialogue_history/当天(分片), 迁移后删旧key"""
        row = self.conn.execute("SELECT value FROM kv WHERE key='dialogue_history'").fetchone()
        if not row:
            return
        key = f"dialogue_history/{time.strftime('%Y-%m-%d')}"
        try:
            old = json.loads(row["value"])
        except json.JSONDecodeError:
            old = []
        if not isinstance(old, list):
            old = []
        exist = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        merged = (json.loads(exist["value"]) if exist else []) + old
        self._rev += 1
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                          (key, json.dumps(merged, ensure_ascii=False), self._rev))
        self.conn.execute("DELETE FROM kv WHERE key='dialogue_history'")
        self.conn.commit()

    def _apply(self, op, kw):
        """写操作(调用方必须已持_wlock). 返回None=成功走默认响应; 返回dict=自定义/失败响应"""
        c = self.conn
        # 事件日志(地基①): append-only记录所有状态变更——事故可回放, 审计可追溯
        def _log(etype, detail):
            c.execute("INSERT INTO events (ts, etype, source, detail) VALUES (?,?,?,?)",
                      (time.time(), etype, str(kw.get("source", "state")), json.dumps(detail, ensure_ascii=False)[:2000]))
        if op == "emit":
            sig = kw["sig"]
            sig.__post_init__()  # 服务端强制clamp
            c.execute("INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?)", sig.to_row())
            _log("signal_emit", {"sig_type": sig.sig_type, "sig_id": sig.sig_id})
        elif op == "kv_set":
            expected = kw.get("expected_revision")
            if expected is not None:
                # P2-2 CAS乐观锁: check-and-set在同一写锁内完成
                row = c.execute("SELECT revision FROM kv WHERE key=?", (kw["key"],)).fetchone()
                cur = row["revision"] if row else 0
                if cur != expected:
                    return {"ok": False, "err": "conflict", "current_revision": cur}
            self._rev += 1
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                      (kw["key"], json.dumps(kw["value"], ensure_ascii=False), self._rev))
            _log("kv_set", {"key": kw["key"], "revision": self._rev})
        elif op == "kv_append":
            # P2-2 原子追加: 值必须是JSON数组(key不存在则新建[]), 否则报错
            row = c.execute("SELECT value FROM kv WHERE key=?", (kw["key"],)).fetchone()
            if row is None:
                arr = []
            else:
                try:
                    arr = json.loads(row["value"])
                except json.JSONDecodeError:
                    arr = None
                if not isinstance(arr, list):
                    return {"ok": False, "err": "value不是JSON数组, 无法append"}
            arr.append(kw["item"])
            # P1-6改进(Manus可恢复压缩标准): 对话史超限不再不可逆删除——
            # 被裁的旧条目先归档到 dialogue_archive/<天>/<序号段>, 需要时可恢复
            if kw["key"].startswith("dialogue_history/") and len(arr) > 200:
                cut = arr[:-100]
                arch_key = f"dialogue_archive/{kw['key'].split('/', 1)[1]}/{int(time.time())}"
                c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                          (arch_key, json.dumps(cut, ensure_ascii=False), self._rev))
                _log("history_archive", {"key": arch_key, "count": len(cut)})
                arr = arr[-100:]
            self._rev += 1
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                      (kw["key"], json.dumps(arr, ensure_ascii=False), self._rev))
        elif op == "drop_signal":
            c.execute("DELETE FROM signals WHERE sig_id=?", (kw["sig_id"],))
        elif op == "del_kv":
            c.execute("DELETE FROM kv WHERE key=?", (kw["key"],))
            _log("kv_del", {"key": kw["key"]})
        elif op == "task_op":
            # Phase4(Q4): 任务生命周期(单写者内原子执行). 合法转移:
            #   create→queued; claim: queued→running(CAS); finish: running→done|failed;
            #   cancel: queued|running→cancelling(协作式, 器官自查退出); requeue: running→queued(退避重试)
            sub = kw["sub"]
            tid = kw["task_id"]
            now = time.time()
            row = c.execute("SELECT value FROM kv WHERE key=?", (f"tasks/{tid}",)).fetchone()
            t = json.loads(row["value"]) if row else None
            if sub == "create" and t is not None:
                return {"ok": False, "err": f"task已存在: {tid}", "current_status": t["status"]}
            if t is None and sub == "create":
                t = {"task_id": tid, "intent": kw.get("intent", ""), "assigned_to": kw.get("assigned_to", ""),
                     "status": "queued", "params": kw.get("params", {}), "steps": [], "result": None,
                     "attempt": 0, "max_retries": 3, "owner_pid": None, "created": now, "finished": None,
                     "idempotency_key": kw.get("idempotency_key", f"{tid}-0")}
                self._rev += 1
                c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                          (f"tasks/{tid}", json.dumps(t, ensure_ascii=False), self._rev))
                c.commit()
                _log("task_create", {"task_id": tid, "assigned_to": t.get("assigned_to")})
                return {"ok": True, "task": t}
            if t is None:
                return {"ok": False, "err": f"task不存在: {tid}"}
            st = t["status"]
            legal = {"claim": (("queued", "suspended"), "running"),
                     "finish": (("running",), kw.get("status", "done")),
                     "cancel": (("queued", "running", "suspended"), "cancelling"),
                     "requeue": (("running",), "queued"),
                     "suspend": (("running",), "suspended")}
            if sub == "suspend":
                t["suspend_reason"] = str(kw.get("reason", ""))[:120]
            if sub not in legal:
                return {"ok": False, "err": f"未知task子操作: {sub}"}
            from_st, to_st = legal[sub]
            if st not in from_st:
                return {"ok": False, "err": f"非法转移: {st} → {to_st}", "current_status": st}
            if sub == "claim":
                t["owner_pid"] = kw.get("owner_pid"); t["started_at"] = now; t["attempt"] += 1
            elif sub == "finish":
                t["result"] = kw.get("result"); t["finished"] = now
            elif sub == "requeue":
                t["attempt"] = kw.get("attempt", t["attempt"])
            t["status"] = to_st
            self._rev += 1
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                      (f"tasks/{tid}", json.dumps(t, ensure_ascii=False), self._rev))
            if sub in ("finish", "cancel"):
                _log(f"task_{sub}", {"task_id": tid, "status": to_st,
                                     "result": str(t.get("result"))[:200]})
            c.commit()
            return {"ok": True, "task": t}
        c.commit()

    def _checkpoint(self, force=False):
        """降频checkpoint: 每30秒或每50操作(先到者). 滚动保留5份."""
        now = time.time()
        if not force and (now - self._last_checkpoint < self.CP_INTERVAL
                          and self._ops_since_cp < self.CP_OPS):
            return None
        ts = time.strftime("%Y%m%d_%H%M%S")
        cp_dir = os.path.join(os.path.dirname(DB_PATH), "checkpoints")
        os.makedirs(cp_dir, exist_ok=True)
        dst = os.path.join(cp_dir, f"state_{ts}_r{self._rev}.db")
        bak = sqlite3.connect(dst)
        self.conn.backup(bak)
        bak.close()
        self._last_checkpoint = now
        self._ops_since_cp = 0
        # 滚动清理: 只留最近5份
        snaps = sorted(f for f in os.listdir(cp_dir) if f.startswith("state_") and f.endswith(".db"))
        for old in snaps[:-self.CP_KEEP]:
            try: os.unlink(os.path.join(cp_dir, old))
            except OSError: pass
        return dst

    def _decay(self):
        # P0 TTL强制过期(P0信号只受TTL/主动drop约束, 不参与下方逐tick衰减删除:
        # 修复意识核阻塞>103s期间主人消息被物理删除的P1-1丢消息)
        self.conn.execute(
            "DELETE FROM signals WHERE sig_type IN ('master_message','risk_trigger') AND created < ?",
            (time.time() - P0_TTL,))
        self.conn.execute(
            "UPDATE signals SET strength=strength*(1-decay) WHERE decay>0 AND sig_type NOT IN ('master_message','risk_trigger')")
        # 防饿死: P2/P3信号保底强度不低于阈值(每个tick复活一次, 意识核轮询处理)
        self.conn.execute(
            "UPDATE signals SET strength = MAX(strength, ?) WHERE sig_type IN ('task_done','loop_progress','heartbeat','loop_error') AND strength < ?",
            (self.threshold * 0.6, self.threshold))
        self.conn.execute(
            "DELETE FROM signals WHERE strength<? AND sig_type NOT IN ('master_message','risk_trigger')",
            (self.threshold*0.5,))
        self.conn.commit()

    def _handle(self, conn):
        """器官连接处理: 协议v2消息帧为4字节大端长度前缀+JSON;
        兼容旧格式(裸JSON以\\n结尾); 单行/单帧超过1MB直接拒绝返回错误"""
        conn.settimeout(300)   # P0修复(2026-09-19): 单连接300s无活动即断, 防死客户端卡死线程(Recv-Q曾堆积5KB)
        f = conn.makefile("rb")
        while True:
            first = f.read(1)
            if not first:
                break
            framed = first != b"{"
            if framed:
                hdr = first + f.read(3)
                if len(hdr) < 4:
                    break
                n = struct.unpack(">I", hdr)[0]
                if n > MAX_MSG:
                    err = json.dumps({"ok": False, "err": "消息超过1MB上限, 拒绝处理"}).encode()
                    conn.sendall(struct.pack(">I", len(err)) + err)
                    break   # 长度不可信, 无法重同步, 断开
                raw = f.read(n)
                if len(raw) < n:
                    break
            else:
                rest = f.readline(MAX_MSG + 1)
                raw = first + rest
                if len(raw) > MAX_MSG and not raw.endswith(b"\n"):
                    err = json.dumps({"ok": False, "err": "消息超过1MB上限, 拒绝处理"}).encode()
                    conn.sendall(err + b"\n")
                    while rest and not rest.endswith(b"\n"):   # 丢弃本行剩余部分, 重同步
                        rest = f.readline(MAX_MSG + 1)
                    continue
            line = raw.strip()
            if not line: continue
            req = {}
            try:
                req = json.loads(line)
                op = req.get("op")
                if op == "emit":
                    s = Signal(sig_type=req["sig_type"], payload=req["payload"],
                               strength=req.get("strength",1.0), decay=req.get("decay",0.3),
                               source=req.get("source",""),
                               sig_id=req.get("sig_id") or uuid.uuid4().hex[:12])
                    with self._wlock:
                        self._apply("emit", {"sig": s})
                    resp = {"ok": True, "sig_id": s.sig_id}
                elif op == "kv_set":
                    with self._wlock:
                        r0 = self._apply("kv_set", req)
                    resp = r0 if isinstance(r0, dict) else {"ok": True, "revision": self._rev}
                elif op == "kv_append":
                    with self._wlock:
                        r0 = self._apply("kv_append", req)
                    resp = r0 if isinstance(r0, dict) else {"ok": True, "revision": self._rev}
                elif op == "kv_get":          # 读也可以走这(单连接串行, 免锁)
                    r = self.conn.execute("SELECT value FROM kv WHERE key=?", (req["key"],)).fetchone()
                    resp = {"ok": True, "value": json.loads(r["value"]) if r else None}
                elif op == "kv_list":         # 前缀扫描(reflection挂起区清扫用)
                    rows = self.conn.execute(
                        "SELECT key, value FROM kv WHERE key LIKE ?", (req.get("prefix","")+"%",)).fetchall()
                    resp = {"ok": True, "items": {r["key"]: json.loads(r["value"]) for r in rows}}
                elif op == "signals":
                    rows = self.conn.execute(
                        "SELECT * FROM signals WHERE strength>? ORDER BY strength DESC LIMIT 20",
                        (self.threshold,)).fetchall()
                    resp = {"ok": True, "signals": [dict(r) for r in rows]}
                elif op == "drop":
                    with self._wlock:
                        self._apply("drop_signal", {"sig_id": req["sig_id"]})
                    resp = {"ok": True}
                elif op == "del_kv":
                    with self._wlock:
                        self._apply("del_kv", {"key": req["key"]})
                    resp = {"ok": True}
                elif op == "task_op":
                    with self._wlock:
                        resp = self._apply("task_op", req)
                    if resp is None:
                        resp = {"ok": True}
                elif op == "checkpoint":
                    with self._wlock:
                        dst = self._checkpoint()
                    resp = {"ok": True, "snapshot": dst}
                elif op == "ping":
                    resp = {"ok": True, "revision": self._rev}
                elif op == "events_query":
                    # 地基①: 事件日志查询(append-only审计). 参数: since_id/etype/limit
                    q = "SELECT event_id, ts, etype, source, detail FROM events WHERE event_id > ?"
                    args = [int(req.get("since_id", 0))]
                    if req.get("etype"):
                        q += " AND etype=?"
                        args.append(req["etype"])
                    q += " ORDER BY event_id ASC LIMIT ?"
                    args.append(min(int(req.get("limit", 100)), 500))
                    with self._wlock:
                        rows = self.conn.execute(q, args).fetchall()
                    resp = {"ok": True, "events": [dict(r) for r in rows]}
                else:
                    resp = {"ok": False, "err": f"unknown op {op}"}
            except Exception as e:
                resp = {"ok": False, "err": str(e)}
            if isinstance(resp, dict) and "request_id" in req:
                resp["request_id"] = req["request_id"]   # 协议v2: 回显request_id供客户端匹配
            raw_resp = json.dumps(resp, ensure_ascii=False).encode()
            if framed:
                conn.sendall(struct.pack(">I", len(raw_resp)) + raw_resp)
            else:
                conn.sendall(raw_resp + b"\n")   # 旧格式请求维持旧格式响应
        conn.close()

    def _worker(self):
        last = time.time()
        while self.alive:
            time.sleep(0.05)
            if time.time()-last >= self.tick:
                with self._wlock:
                    self._decay()
                last = time.time()

    def run(self):
        threading.Thread(target=self._worker, daemon=True, name="state-worker").start()
        print(f"[State服务] 监听 {SOCK_PATH} · revision={self._rev}")
        while self.alive:
            conn, _ = self.sock.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

class StateClient:
    """器官侧句柄: 自动重连+指数退避(修复: 服务重启时器官不会失联死)"""
    def __init__(self, sock_path=SOCK_PATH, timeout=10, max_retries=5):
        self.sock_path = sock_path
        self.timeout = timeout
        self.max_retries = max_retries
        self._connect()

    def _connect(self):
        import time as _t
        delay = 0.5
        last_err = None
        for attempt in range(self.max_retries):
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                self.sock.connect(self.sock_path)
                self.f = self.sock.makefile("rb")
                return
            except (ConnectionRefusedError, FileNotFoundError) as e:
                last_err = e
                _t.sleep(delay)
                delay = min(delay * 2, 8)   # 0.5→1→2→4→8s指数退避
        raise ConnectionError(f"State服务连不上({self.max_retries}次): {last_err}")

    def _reconnect(self):
        try: self.sock.close()
        except: pass
        self._connect()

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.f.read(n - len(buf))
            if not chunk:
                raise ConnectionError("连接中断")
            buf += chunk
        return buf

    def _req(self, _raise_on_err=True, **req):
        req["request_id"] = uuid.uuid4().hex[:16]   # 协议v2: 请求带id, 响应回显匹配
        payload = json.dumps(req, ensure_ascii=False).encode()
        if len(payload) > MAX_MSG:
            raise RuntimeError("请求超过1MB上限")
        frame = struct.pack(">I", len(payload)) + payload
        for attempt in range(2):   # 首次失败重连一次重试
            try:
                self.sock.sendall(frame)
                first = self._read_exact(1)
                if first == b"{":            # 旧服务降级: 裸JSON行响应
                    r = json.loads(first + self.f.readline(MAX_MSG + 1))
                else:
                    n = struct.unpack(">I", first + self._read_exact(3))[0]
                    if n > MAX_MSG:
                        raise RuntimeError("响应超过1MB上限")
                    r = json.loads(self._read_exact(n))
                if r.get("request_id") != req["request_id"]:
                    raise RuntimeError(f"request_id不匹配: {r.get('request_id')}")
                if _raise_on_err and not r.get("ok"):
                    raise RuntimeError(r.get("err"))
                return r
            except (ConnectionError, BrokenPipeError, FileNotFoundError,
                    json.JSONDecodeError, socket.timeout, TimeoutError):
                # P0修复(2026-09-19): socket.timeout也必须重连! 
                # 超时后连接处于半包状态, 复用=协议错位永久卡死(core曾22s一次timeout循环3188次/天)
                if attempt == 0:
                    self._reconnect()
                    continue
                raise

    def emit(self, sig_type, payload, strength=1.0, decay=0.3, source="", sig_id=None):
        import uuid
        if not sig_id:
            sig_id = uuid.uuid4().hex[:12]   # 幂等key: 重连重发同id不产生重复信号
        return self._req(op="emit", sig_type=sig_type, payload=payload,
                         strength=strength, decay=decay, source=source, sig_id=sig_id)
    def kv_set(self, key, value, expected_revision=None):
        if expected_revision is None:
            return self._req(op="kv_set", key=key, value=value)
        # P2-2 CAS模式: 冲突不抛异常, 返回{"ok":False,"err":"conflict","current_revision":N}
        return self._req(_raise_on_err=False, op="kv_set", key=key, value=value,
                         expected_revision=expected_revision)
    def kv_append(self, key, item): return self._req(op="kv_append", key=key, item=item)
    def kv_get(self, key):        return self._req(op="kv_get", key=key).get("value")
    def kv_list(self, prefix=""): return self._req(op="kv_list", prefix=prefix).get("items", {})
    def recall_archived(self, day: str = ""):
        """读回被压缩归档的对话史(Manus可恢复压缩)——day缺省返回全部归档段"""
        prefix = f"dialogue_archive/{day}" if day else "dialogue_archive/"
        return self.kv_list(prefix)
    def get_history_range(self, days=1):
        """P2-4: 读最近N天对话历史分片(dialogue_history/YYYY-MM-DD), 按日期升序合并返回"""
        items = self.kv_list("dialogue_history/")
        out = []
        for i in range(days - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            v = items.get(f"dialogue_history/{day}")
            if isinstance(v, list):
                out.extend(v)
        return out
    def kv_del(self, key):        return self._req(op="del_kv", key=key)

    # ---- Phase4(Q4): 任务生命周期 ----
    def task_create(self, task_id, intent="", assigned_to="", params=None, idempotency_key=None):
        return self._req(op="task_op", sub="create", task_id=task_id, intent=intent,
                         assigned_to=assigned_to, params=params or {},
                         idempotency_key=idempotency_key or f"{task_id}-0")
    def task_claim(self, task_id, owner_pid=None):
        return self._req(op="task_op", sub="claim", task_id=task_id, owner_pid=owner_pid)
    def task_finish(self, task_id, status="done", result=None):
        return self._req(op="task_op", sub="finish", task_id=task_id, status=status, result=result)
    def task_cancel(self, task_id):
        return self._req(op="task_op", sub="cancel", task_id=task_id)
    def task_requeue(self, task_id, attempt):
        return self._req(op="task_op", sub="requeue", task_id=task_id, attempt=attempt)
    def task_suspend(self, task_id, reason=""):
        return self._req(op="task_op", sub="suspend", task_id=task_id, reason=reason)
    def tasks_by_status(self, status):
        """按状态扫任务(BDI重考虑用)"""
        out = []
        for k, v in (self.kv_list("tasks/") or {}).items():
            if isinstance(v, dict) and v.get("status") == status:
                out.append(v)
        return out
    def task_get(self, task_id):
        return self.kv_get(f"tasks/{task_id}")
    def events_query(self, since_id=0, etype=None, limit=100):
        return self._req(op="events_query", since_id=since_id, etype=etype, limit=limit)
    def signals(self):            return self._req(op="signals").get("signals")
    def drop(self, sig_id):       return self._req(op="drop", sig_id=sig_id)
    def attention(self, focus_task=None):
        sigs = self.signals()
        for s in sigs:
            if focus_task and focus_task in json.dumps(s["payload"]):
                s["strength"] *= 1.5
        sigs.sort(key=lambda s: -s["strength"])
        return sigs
    def checkpoint(self):
        return self._req(op="checkpoint")
    def close(self):
        try: self.sock.close()
        except: pass

if __name__ == "__main__":
    StateServer().run()
