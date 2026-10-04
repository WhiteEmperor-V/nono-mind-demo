#!/usr/bin/env python3
"""nono-canvas: 诺诺的画布服务 (OpenClaw canvas 同款理念, Server1 版)
- 画布页: http://<YOUR-TAILSCALE-IP>:8765  (主人手机 Safari 打开, 加到主屏幕)
- 推送 API: POST /push  {"type": "image|text|html|progress", ...}  (诺诺的工具调用)
- SSE 实时推送, 画布页自动更新
"""
import asyncio, json, time, os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

CANVAS_DIR = Path("/root/nono-mind/canvas")
CANVAS_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = CANVAS_DIR / "state.json"
IMG_DIR = CANVAS_DIR / "images"
IMG_DIR.mkdir(parents=True, exist_ok=True)
PAGES_DIR = CANVAS_DIR / "pages"
PAGES_DIR.mkdir(parents=True, exist_ok=True)

# 全局SSE订阅队列
_subscribers: list = []

def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"items": []}

def _save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))

app = FastAPI(title="nono-canvas")

@app.post("/push")
async def push(req: Request):
    """诺诺推送内容: {"type": "text"|"image"|"html"|"progress", "title":..., "data":...}"""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "err": "bad json"}, status_code=400)
    t = body.get("type", "text")
    item = {"ts": time.time(), "type": t, "title": body.get("title", ""), }

    if t == "image":
        src = body.get("path")  # 本地图片路径 → 拷进画布目录
        if src and os.path.exists(src):
            import shutil
            dst = IMG_DIR / f"{int(time.time()*1000)}_{Path(src).name}"
            shutil.copy(src, dst)
            item["url"] = f"/images/{dst.name}"
        else:
            item["url"] = body.get("url", "")
    elif t == "html":
        item["html"] = body.get("html", "")[:20000]
    elif t == "page":
        # 完整网页: 拷进pages目录, iframe嵌入展示
        src = body.get("path")
        if src and os.path.exists(src):
            import shutil
            name = body.get("name") or Path(src).stem
            dst = PAGES_DIR / f"{name}.html"
            shutil.copy(src, dst)
            item["page_url"] = f"/pages/{name}.html"
        elif body.get("url"):
            item["page_url"] = body["url"]
    elif t == "progress":
        item["percent"] = body.get("percent", 0)
        item["label"] = body.get("label", "")
    elif t == "chart":
        item["chart"] = body.get("chart", {})  # {labels:[], values:[]}
    elif t == "actions":
        item["buttons"] = body.get("buttons", [])  # [{"label":"...", "action":"...", "value":"..."}]
    else:
        item["text"] = str(body.get("data", body.get("text", "")))[:5000]

    state = _load_state()
    state["items"].append(item)
    state["items"] = state["items"][-50:]  # 保留最近50条
    _save_state(state)

    # SSE广播
    evt = json.dumps({"event": "update", "item": item}, ensure_ascii=False)
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(evt)
        except Exception:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)
    return {"ok": True, "total": len(state["items"])}

@app.post("/clear")
async def clear():
    _save_state({"items": []})
    for q in list(_subscribers):
        q.put_nowait(json.dumps({"event": "clear"}))
    return {"ok": True}

@app.post("/action")
async def action(req: Request):
    """画布上的按钮/输入 → 触发诺诺动作 → 结果走微信回复"""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    action = body.get("action", "")
    value = body.get("value", "")
    # 投递到State信号队列(意识核拾取) — 标记来自画布
    import sys
    sys.path.insert(0, "/root/nono-mind")
    try:
        from state.server import StateClient
        c = StateClient()
        c.emit("master_message", {"text": f"[画布] {action}: {value}", "from_canvas": True},
               source="nono_canvas")
        c.close()
        return {"ok": True, "note": "已交给诺诺"}
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)

@app.get("/pages/{name}")
async def pages(name: str):
    p = PAGES_DIR / name
    if p.exists():
        return FileResponse(p)
    return JSONResponse({"err": "not found"}, status_code=404)

@app.get("/images/{name}")
async def images(name: str):
    p = IMG_DIR / name
    if p.exists():
        return FileResponse(p)
    return JSONResponse({"err": "not found"}, status_code=404)

@app.get("/events")
async def events():
    from sse_starlette.sse import EventSourceResponse
    q = asyncio.Queue()
    _subscribers.append(q)
    async def gen():
        yield {"data": json.dumps({"event": "hello", "items": _load_state()["items"]}, ensure_ascii=False)}
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=25)
                yield {"data": evt}
            except asyncio.TimeoutError:
                yield {"data": json.dumps({"event": "ping"})}  # keepalive
    return EventSourceResponse(gen())

@app.get("/", response_class=HTMLResponse)
async def canvas():
    return CANVAS_HTML

CANVAS_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<script src='https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js'></script>
<title>诺诺画布</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --line:#21262d; --accent:#58a6ff; --text:#e6edf3; --dim:#8b949e; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,"PingFang SC",sans-serif; padding:14px; min-height:100vh; }
  header { display:flex; align-items:center; gap:8px; padding:6px 4px 14px; }
  .dot { width:9px; height:9px; border-radius:50%; background:#3fb950; box-shadow:0 0 8px #3fb95088; }
  .dot.off { background:#f85149; box-shadow:0 0 8px #f8514988; }
  h1 { font-size:17px; font-weight:600; }
  #sub { color:var(--dim); font-size:12px; margin-left:auto; }
  #feed { display:flex; flex-direction:column; gap:12px; max-width:760px; margin:0 auto; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; animation:in .35s ease; }
  @keyframes in { from { opacity:0; transform:translateY(8px);} to { opacity:1; transform:none;} }
  .card .t { font-size:12px; color:var(--accent); margin-bottom:8px; font-weight:600; letter-spacing:.5px; }
  .card img { max-width:100%; border-radius:10px; display:block; }
  .card .txt { font-size:15px; line-height:1.65; white-space:pre-wrap; }
  .card .html { font-size:14px; line-height:1.5; }
  .bar { height:8px; background:var(--line); border-radius:4px; overflow:hidden; margin-top:8px; }
  .bar i { display:block; height:100%; background:linear-gradient(90deg,#1f6feb,#58a6ff); border-radius:4px; transition:width .6s; }
  .lbl { font-size:13px; color:var(--dim); }
  .ts { font-size:11px; color:var(--dim); margin-top:8px; text-align:right; }
  .btns { display:flex; flex-wrap:wrap; gap:8px; margin-top:4px; }
  .abtn { background:#21262d; color:var(--text); border:1px solid #30363d; border-radius:10px; padding:10px 16px; font-size:14px; cursor:pointer; transition:.15s; }
  .abtn:active { background:var(--accent); color:#000; }
  .chart { width:100%; height:260px; }
  #empty { text-align:center; color:var(--dim); padding:80px 20px; font-size:14px; }
</style>
</head>
<body>
<header><span class="dot" id="dot"></span><h1>诺诺画布</h1><span id="sub"></span></header>
<div id="feed"><div id="empty">等诺诺往画布上放东西…</div></div>
<script>
const feed = document.getElementById('feed');
const empty = document.getElementById('empty');
const dot = document.getElementById('dot');
const sub = document.getElementById('sub');

function fmt(ts){ const d=new Date(ts*1000); return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0'); }
function render(item, prepend=true){
  const el = document.createElement('div');
  el.className = 'card';
  let inner = '';
  if (item.title) inner += `<div class="t">${item.title}</div>`;
  if (item.type==='image' && item.url) inner += `<img src="${item.url}" loading="lazy">`;
  if (item.type==='text') inner += `<div class="txt">${item.text}</div>`;
  if (item.type==='html') inner += `<div class="html">${item.html}</div>`;
  if (item.type==='progress') inner += `<div class="lbl">${item.label||''} ${item.percent}%</div><div class="bar"><i style="width:${item.percent}%"></i></div>`;
  if (item.type==='page' && item.page_url) inner += `<iframe src="${item.page_url}" style="width:100%;height:70vh;border:1px solid var(--line);border-radius:10px;background:#fff"></iframe>`;
  if (item.type==='chart' && item.chart) { inner += `<div class="chart" id="ch${item.ts}"></div>`; setTimeout(()=>drawChart(item), 50); }
  if (item.type==='actions' && item.buttons) {
    inner += '<div class="btns">' + item.buttons.map((b,i)=>
      `<button class="abtn" data-action="${b.action||b.label}" data-value="${b.value||''}">${b.label||b}</button>`).join('') + '</div>';
  }
  inner += `<div class="ts">${fmt(item.ts)}</div>`;
  el.innerHTML = inner;
  if (prepend) feed.prepend(el); else feed.append(el);
  empty.style.display = 'none';
}
function loadAll(items){
  feed.innerHTML = '';
  if (!items || !items.length){ feed.appendChild(empty); return; }
  [...items].reverse().forEach(i => render(i, false));
}

feed.addEventListener('click', (ev) => {
  const btn = ev.target.closest('.abtn');
  if (!btn) return;
  btn.disabled = true; btn.textContent = '…';
  fetch('/action', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action: btn.dataset.action, value: btn.dataset.value})})
    .then(r=>r.json()).then(d=>{ btn.textContent = d.ok ? '✓' : '×'; setTimeout(()=>location.reload(), 800); });
});
function drawChart(item){
  const el = document.getElementById('ch'+item.ts);
  if (!el || !window.echarts) return;
  const ch = echarts.init(el, 'dark');
  ch.setOption({ backgroundColor:'transparent',
    xAxis:{type:'category', data:item.chart.labels||[]},
    yAxis:{type:'value'},
    series:[{type:'line', data:item.chart.values||[], smooth:true, lineStyle:{color:'#58a6ff'}, areaStyle:{opacity:.15}}]});
  window.addEventListener('resize', ()=>ch.resize());
}

const es = new EventSource('/events');
es.onopen = () => { dot.classList.remove('off'); sub.textContent = '已连接'; };
es.onerror = () => { dot.classList.add('off'); sub.textContent = '重连中…'; };
es.onmessage = (e) => {
  try {
    const d = JSON.parse(e.data);
    if (d.event === 'hello') loadAll(d.items);
    else if (d.event === 'update') render(d.item);
    else if (d.event === 'clear') loadAll([]);
  } catch(err){}
};
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="<YOUR-TAILSCALE-IP>", port=8765, log_level="warning")  # P0-1修复: 仅Tailscale内网可达
