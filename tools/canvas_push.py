#!/usr/bin/env python3
"""诺诺的画布推送工具
用法:
  canvas_push.py text "标题" "内容"
  canvas_push.py image "标题" /path/to/img.png
  canvas_push.py progress "任务名" 45
  canvas_push.py html "标题" "<b>html</b>"
  canvas_push.py clear
"""
import json, sys, urllib.request

URL = "http://127.0.0.1:8765/push"

def push(payload):
    req = urllib.request.Request(URL, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return r.get("ok")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "text"
    if cmd == "clear":
        req = urllib.request.Request("http://127.0.0.1:8765/clear", data=b"{}", headers={"Content-Type": "application/json"})
        print("清空:", json.loads(urllib.request.urlopen(req, timeout=10).read()).get("ok"))
    elif cmd == "text":
        print("推送:", push({"type": "text", "title": sys.argv[2], "data": sys.argv[3]}))
    elif cmd == "image":
        print("推送:", push({"type": "image", "title": sys.argv[2], "path": sys.argv[3]}))
    elif cmd == "progress":
        print("推送:", push({"type": "progress", "label": sys.argv[2], "percent": int(sys.argv[3])}))
    elif cmd == "html":
        print("推送:", push({"type": "html", "title": sys.argv[2], "html": sys.argv[3]}))
