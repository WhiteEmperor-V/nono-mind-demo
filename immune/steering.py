#!/usr/bin/env python3
"""Pi式steering双队列(Python版): coder执行中接受主人中途注入的指令
架构符合性(主人铁律): 队列内容来自State的task_steer/{task_id}——只有核心loop/主人微信能写,
coder只读自己的队列, 不和其它器官私通。

两种注入:
- steer: 打断式, 注入下一轮(主人补充要求/转向) —— Pi的steeringQueue
- followup: 当前任务完成后追加的新任务 —— Pi的followUpQueue
"""
import time

KV_STEER = "task_steer/"      # kv_append目标: task_steer/{task_id} = [{mode: steer|followup, text}]

def poll(c, task_id: str) -> dict:
    """拉取并清空该任务的注入队列. 返回{steer: [..], followup: [..]}"""
    key = f"{KV_STEER}{task_id}"
    items = c.kv_get(key) or []
    if not items:
        return {"steer": [], "followup": []}
    c.kv_del(key)
    out = {"steer": [], "followup": []}
    for it in items:
        if not isinstance(it, dict):
            continue  # 垃圾数据容错
        mode = it.get("mode", "steer")
        text = str(it.get("text", "")).strip()
        if text:
            out["steer" if mode == "steer" else "followup"].append(text[:300])
    return out

def push(c, task_id: str, text: str, mode: str = "steer"):
    """外部(核心loop/主人微信经核心)注入. 器官自己不调用这个"""
    key = f"{KV_STEER}{task_id}"
    arr = c.kv_get(key) or []
    if not isinstance(arr, list):
        arr = []
    arr.append({"at": time.time(), "mode": mode, "text": text[:300]})
    c.kv_set(key, arr)

def render_nudge(steer: list) -> str:
    """渲染成注入prompt的纠偏段(Pi: 注入消息在下轮assistant响应前可见)"""
    if not steer:
        return ""
    lines = [f"【主人中途指示】{s}" for s in steer]
    return "\n".join(lines) + "\n(以上指示立即生效, 调整后续步骤)"
