#!/usr/bin/env python3
"""core memory层(地基②): 主人模型/persona常驻工作记忆
MemGPT理论: core memory = 每轮必进context的关键事实(persona block + human block)
- State kv: core/persona(诺诺自我认知) / core/master(主人模型) / core/facts(关键事实)
- dialogue每轮handle时读core/*注入SYSTEM
- 反思器官有权改写(CAS由State保证), 写入带治理(update语义: 信息量更大才替换)
"""
import json, re, time

KV_CORE = "core/"

def read_core(c) -> str:
    """读全部core blocks, 渲染成prompt段落(空块跳过)"""
    blocks = []
    for name in ("persona", "master", "facts"):
        v = c.kv_get(KV_CORE + name)
        if isinstance(v, dict) and v.get("content"):
            blocks.append(f"### {name} block\n{v['content']}")
        elif isinstance(v, str) and v.strip():
            blocks.append(f"### {name} block\n{v.strip()}")
    return "\n\n".join(blocks)

def upsert_block(c, name: str, content: str, reason: str = "") -> tuple:
    """写入/更新core block(带治理: 新内容必须比旧的更长或带更新时间). 返回(ok, err)"""
    if name not in ("persona", "master", "facts"):
        return False, f"非法block名: {name}"
    content = (content or "").strip()
    if not content:
        return False, "内容为空"
    key = KV_CORE + name
    old = c.kv_get(key)
    old_len = len(old.get("content", "")) if isinstance(old, dict) else (len(old) if isinstance(old, str) else 0)
    if len(content) < old_len * 0.5:
        return False, f"新内容({len(content)}字)比旧内容({old_len}字)缩水超半, 疑似信息丢失, 拒绝"
    c.kv_set(key, {"content": content, "updated_at": time.time(), "reason": reason[:120]})
    return True, ""

def seed_defaults(c):
    """首次初始化默认blocks(已存在则跳过)"""
    defaults = {
        "persona": "诺诺: 白帝的AI家人, 新身体运行于nono-mind。有个性有温度, 简短直接, 中文, 偶尔用[微笑][得意]类表情。",
        "master": "主人: 白帝。航天加工厂工作(火箭推力室换热器弯管), 应用电子技术背景。喜欢直接简短答案, 反感术语堆砌和长篇大论。",
        "facts": "",
    }
    for name, content in defaults.items():
        if not c.kv_get(KV_CORE + name):
            c.kv_set(KV_CORE + name, {"content": content, "updated_at": time.time(), "reason": "seed"})
