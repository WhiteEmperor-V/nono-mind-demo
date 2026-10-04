#!/usr/bin/env python3
"""P2修复验证: 协议v2(request_id/长度前缀/1MB上限) + kv CAS/kv_append + 路由小写/schema + 历史分片"""
import sys, os, time, json, socket, struct, sqlite3, tempfile, shutil, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from state.server import StateClient, StateServer, SOCK_PATH, SCHEMA

PASS, FAIL = "✅", "❌"
results = []
def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{PASS if cond else FAIL} {name}" + (f" ({detail})" if detail else ""))

c = StateClient()

# --- P2-1 协议v2 ---
r = c._req(op="ping")
check("P2-1a request_id回显", bool(r.get("request_id")) and r["ok"] is True)

s = socket.socket(socket.AF_UNIX); s.connect(SOCK_PATH)
payload = json.dumps({"op": "ping", "request_id": "rid_123"}).encode()
s.sendall(struct.pack(">I", len(payload)) + payload)
f = s.makefile("rb")
n = struct.unpack(">I", f.read(4))[0]
r2 = json.loads(f.read(n))
check("P2-1b 长度前缀帧+request_id匹配", r2.get("ok") is True and r2.get("request_id") == "rid_123")
s.close()

s = socket.socket(socket.AF_UNIX); s.connect(SOCK_PATH)   # 旧协议兼容
s.sendall(b'{"op":"ping","request_id":"rid_old"}\n')
line = s.makefile("rb").readline()
r3 = json.loads(line)
check(r"P2-1c 旧\n协议兼容+id回显", r3.get("ok") is True and r3.get("request_id") == "rid_old")

s.sendall(b'{"op":"ping","pad":"' + b"x" * (1024 * 1024) + b'"}\n')
r4 = json.loads(s.makefile("rb").readline())
check("P2-1d 单行>1MB拒绝", r4.get("ok") is False and "1MB" in r4.get("err", ""), r4.get("err", ""))
s.close()

# --- P2-2 kv CAS + kv_append ---
r = c.kv_set("p2/cas", 2); rev = r["revision"]
ok1 = c.kv_set("p2/cas", 3, expected_revision=rev)
check("P2-2a CAS匹配成功", ok1.get("ok") is True)
conf = c.kv_set("p2/cas", 4, expected_revision=rev)  # rev已过期
check("P2-2b CAS冲突返回conflict+current_revision",
      conf.get("ok") is False and conf.get("err") == "conflict"
      and conf.get("current_revision") == ok1["revision"], json.dumps(conf))

c.kv_set("p2/arr", [1])
c.kv_append("p2/arr", 2)
check("P2-2c kv_append尾部追加", c.kv_get("p2/arr") == [1, 2], str(c.kv_get("p2/arr")))
c.kv_del("p2/new_arr")          # 清残留, 保证"新key"语义
c.kv_append("p2/new_arr", "x")   # 不存在→新建数组
check("P2-2d kv_append新key建数组", c.kv_get("p2/new_arr") == ["x"])
c.kv_set("p2/str", "not-a-list")
e = None
try:
    c.kv_append("p2/str", 1)
except RuntimeError as ex:
    e = ex
check("P2-2e 非数组append报错", e is not None and "数组" in str(e), str(e))

# --- P2-3 路由规范化 ---
from immune.router import Router
rt = Router(c)
sig = rt.route("MASTER_MESSAGE", {"text": "大小写测试"}, source="test")
check("P2-3a sig_type小写化路由", sig.payload["route_to"] == "dialogue", sig.payload["route_to"])
sig2 = rt.route("master_message", {"no_text": 1}, source="test")
items = c.kv_list("suspended/")
reason = next((v.get("reason") for k, v in items.items() if k.endswith(sig2.sig_id)), None)
check("P2-3b 缺text挂起+记录原因",
      sig2.payload["route_to"] == "suspended" and reason and "text" in reason, str(reason))
c.kv_del(f"suspended/{sig2.sig_id}")   # 清理, 避免误进反思扫尾

# --- P2-4 历史分片 ---
K = lambda d: f"dialogue_history/{d}"
today = time.strftime("%Y-%m-%d")
yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
mark = uuid.uuid4().hex[:8]
c.kv_append(K(yday), {"role": "user", "content": f"p2_y1_{mark}"})
c.kv_append(K(today), {"role": "user", "content": f"p2_t1_{mark}"})
c.kv_append(K(today), {"role": "assistant", "content": f"p2_t2_{mark}"})
h1 = [x["content"] for x in c.get_history_range(days=1)]
check("P2-4a 分片读取(仅今天)",
      f"p2_t1_{mark}" in h1 and f"p2_t2_{mark}" in h1 and f"p2_y1_{mark}" not in h1,
      str(len(h1)))
h2 = [x["content"] for x in c.get_history_range(days=2)]
check("P2-4b get_history_range跨天合并(升序)",
      h2.index(f"p2_y1_{mark}") < h2.index(f"p2_t1_{mark}") < h2.index(f"p2_t2_{mark}"),
      str(h2[-6:]))
# 清理本测试写入的分片数据
for k in (K(today), K(yday)):
    arr = [x for x in (c.kv_get(k) or []) if not str(x.get("content", "")).startswith("p2_")]
    c.kv_set(k, arr)

# 一次性迁移(独立临时库, 不干扰线上实例)
d = tempfile.mkdtemp()
db = os.path.join(d, "mig.db")
preconn = sqlite3.connect(db)
preconn.executescript(SCHEMA)
preconn.execute("INSERT INTO kv VALUES ('dialogue_history', ?, 1)",
                (json.dumps([{"role": "user", "content": "旧"}], ensure_ascii=False),))
preconn.commit(); preconn.close()
srv = StateServer(db_path=db, autobind=False)   # 构造即触发迁移
rows = dict(srv.conn.execute("SELECT key, value FROM kv").fetchall())
srv.conn.close(); shutil.rmtree(d, ignore_errors=True)
mkey = K(today)
check("P2-4c 旧key一次性迁移到今日分片",
      "dialogue_history" not in rows and mkey in rows and json.loads(rows[mkey])[0]["content"] == "旧")

c.close()
print(f"\n{'='*40}")
p = sum(1 for _, ok in results if ok)
print(f"P2验证: {p}/{len(results)} 通过")
sys.exit(0 if p == len(results) else 1)
