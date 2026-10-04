#!/usr/bin/env python3
"""心跳插件: 每10秒发一个heartbeat信号, 证明插件系统活了"""
import sys, time, json, socket

SOCK = "/tmp/nono_state.sock"
def client():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    return s

def req(s, **r):
    s.sendall((json.dumps(r)+"\n").encode())
    return json.loads(s.makefile("r").readline())

i = 0
s = client()
while True:
    try:
        req(s, op="emit", sig_type="heartbeat",
            payload={"n": i}, strength=0.6, decay=0.05, source="heartbeat_plugin")
        i += 1
    except Exception:
        s = client()
    time.sleep(10)
