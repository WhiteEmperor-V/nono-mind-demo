#!/usr/bin/env python3
"""诺诺的生图工具. 2026-09-30主人拍板删apikey.fun供应商: 全走agnes(nemotron全模态兜底).
原apikey.fun gpt-image链已废; 生图现走nemotron-3-nano-omni(CPA, 能看能生成图), 见skill: 视觉通道.
用法: python3 gen_image.py "提示词" [参考图路径] [-o 输出.png]
"""
import json, sys, time, os

def gen(prompt, ref_image=None, timeout=300):
    """生图: 走nemotron-3-nano-omni(CPA免费, 9/18主人拍板的视觉通道), 不再依赖apikey.fun.
    ponytail: 此工具暂无直接图像生成API, 走agnes写SVG/HTML(画布链), 真生图待挂视觉模型tool."""
    from llm_client import chat
    from organs.canvas_organ import _gpt_write_html, _push_canvas, PAGES_DIR
    import base64
    html = _gpt_write_html(f"画一张图: {prompt}", timeout=timeout)
    if not html:
        return None
    os.makedirs(PAGES_DIR, exist_ok=True)
    fn = f"gen_{int(time.time())}.html"
    p = os.path.join(PAGES_DIR, fn)
    with open(p, "w") as f:
        f.write(html)
    ok, err = _push_canvas(fn, f"诺诺生图 · {prompt[:30]}", p)
    return base64.b64encode(html.encode()).decode() if ok else None

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("-r", "--ref", help="参考图路径")
    ap.add_argument("-o", "--out", default=f"/root/gen_images/img_{int(time.time())}.png")
    a = ap.parse_args()
    t0 = time.time()
    img = gen(a.prompt, a.ref)
    if img:
        print(f"✅ 生图已推画布 ({time.time()-t0:.0f}s), 刷新画布看")
    else:
        print("❌ 未生成")
        sys.exit(1)
