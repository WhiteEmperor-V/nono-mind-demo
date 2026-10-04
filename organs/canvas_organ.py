#!/usr/bin/env python3
"""画布器官(canvas_organ): 画布创作能力(从dialogue迁出, Phase3-Q2)
职责: 收task_dispatch{target:canvas, params:{demand}} → LLM写HTML → 推nono-canvas → 状态写State
能力声明(注册进capabilities): 画布创作/生成网页/按需求画图页面
"""
import json, time, os, urllib.request

KV_LAST = "canvas/last_creation"
PAGES_DIR = "/root/nono-mind/canvas/pages"
CANVAS_PUSH_URL = "http://<YOUR-TAILSCALE-IP>:8765/push"

from immune.plugins import capabilities_from_manifest, track_progress

# 票1: 能力跟插件走——从 plugins/canvas/manifest.yaml 读
CAPABILITIES = capabilities_from_manifest("canvas")

def _gpt_write_html(prompt: str, timeout: int = 180) -> str:
    """画布创作: 全走agnes(2026-09-30主人拍板删apikey.fun供应商). agnes-flash写HTML, 限流退nemotron兜底."""
    from llm_client import chat
    html = chat([{"role": "user", "content": (
        "你是HTML代码生成引擎。输出一个完整的单文件HTML(内联CSS/JS)。"
        "只输出<!DOCTYPE html>开头的代码本身, 禁止任何解释、设计说明或思考过程。\n\n"
        + prompt)}], max_tokens=4000, timeout=timeout)
    html = _strip_fence(str(html))
    i = html.find("<!DOCTYPE"); i2 = html.find("<html")
    s = i if i >= 0 else i2
    if s >= 0:
        e = html.rfind("</html>")
        html = html[s:e+7] if e > s else html[s:]
    return html.strip()

def _strip_fence(s: str) -> str:
    """剥掉markdown代码围栏(反模式: LLM偶尔裹```html)"""
    if s.startswith("```"):
        lines = s.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        s = "\n".join(lines)
    return s.strip()

def _push_canvas(name: str, title: str, path: str) -> tuple:
    """推送页面到画布服务. 返回(ok, err)."""
    try:
        body = json.dumps({"type": "page", "title": title, "path": path, "name": name}).encode()
        req = urllib.request.Request(CANVAS_PUSH_URL, data=body,
            headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        # ponytail: 校验画布接口真返回ok才算推成功(防"只发起没收到"的空口成功), 响应异常仍认ok
        try:
            rdata = json.loads(resp.read())
            if rdata.get("ok") is False:
                return False, f"画布拒绝: {str(rdata)[:80]}"
        except Exception:
            pass
        return True, ""
    except Exception as e:
        return False, str(e)[:120]

def create(demand: str, c) -> str:
    """画布创作主入口(由大loop task_dispatch触发). demand=完整创作需求."""
    record = {"at": time.time(), "subject": demand[:120]}
    try:
        html = _gpt_write_html(demand)
        if not html or "<" not in html:
            raise ValueError("LLM输出不是HTML")
        html = _strip_fence(html)
        os.makedirs(PAGES_DIR, exist_ok=True)
        fname = f"creation_{int(time.time())}.html"
        path = os.path.join(PAGES_DIR, fname)
        with open(path, "w") as f:
            f.write(html)
        record["name"] = fname
        record["path"] = path
        ok, err = _push_canvas(fname, f"诺诺创作 · {demand[:40]}", path)
        record["ok"] = True
        record["pushed"] = ok
        if ok:
            c.kv_set(KV_LAST, record)
            return f"画好了！已经推到画布上, 主人刷新看看~[得意]"
        else:
            record["error"] = f"画布推送失败: {err}"
            c.kv_set(KV_LAST, record)
            return (f"作品完成了(文件已保存), 但推到画布时网络抖了一下——"
                    f"主人稍等诺诺重推, 或者问一句'画布状态'我查给你~[害羞]")
    except Exception as e:
        record["ok"] = False
        record["error"] = str(e)[:200]
        c.kv_set(KV_LAST, record)
        if "超时" in str(e) or isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
            return "创作超时了——我等了3分钟模型还没交出成品, 先把这次放弃了, 主人稍后再让我试一次?[委屈]"
        return f"创作失败了……诺诺老实交代: {str(e)[:80]}。主人稍后再试一次?[难过]"

create = track_progress("canvas", lambda demand, c: "画布:" + str(demand)[:50])(create)   # 票1: 写states/canvas/progress
