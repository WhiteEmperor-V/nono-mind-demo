#!/usr/bin/env python3
"""能力注册表(immune/capabilities.py): 器官/插件的capabilities声明与校验入库
Phase1(Q3): 插件自报的描述必须经本校验(非空/含"何时不用"/params_schema合法)才入State。
注册表 = 大loop能力匹配的数据源。插件不可直写State注册。
"""
import json, re

KV_NS = "capabilities/"
# 反模式黑名单(Anthropic工程博客实证): 描述必须含用途边界, 参数必须具名
_DESC_MIN_LEN = 20
_FORBIDDEN_RE = re.compile(r"(何时不用|不能|不适合|not for)", re.I)

def validate_capability(cap: dict) -> tuple:
    """校验一条能力声明. 返回(ok: bool, err: str)."""
    if not isinstance(cap, dict):
        return False, "capability必须是dict"
    for field in ("organ", "name", "description"):
        v = cap.get(field)
        if not v or not isinstance(v, str) or not v.strip():
            return False, f"缺少必填字段: {field}"
    d = cap["description"].strip()
    if len(d) < _DESC_MIN_LEN:
        return False, f"description过短(<{_DESC_MIN_LEN}字): 要写清干什么/何时用/何时不用"
    if not _FORBIDDEN_RE.search(d):
        return False, "description缺少'何时不用/不能'边界说明(Anthropic反模式: 无边界的描述导致误匹配)"
    ps = cap.get("params_schema", {})
    if ps and not isinstance(ps, dict):
        return False, "params_schema必须是dict"
    ex = cap.get("examples", [])
    if ex and (not isinstance(ex, list) or not all(isinstance(x, str) for x in ex)):
        return False, "examples必须是字符串数组"
    return True, ""

def register(c, cap: dict) -> tuple:
    """校验并注册一条能力到State. 返回(ok, err)."""
    ok, err = validate_capability(cap)
    if not ok:
        return False, err
    organ = cap["organ"].strip()
    key = KV_NS + organ
    entry = {
        "organ": organ,
        "name": cap["name"],
        "description": cap["description"].strip(),
        "params_schema": cap.get("params_schema", {}),
        "examples": cap.get("examples", []),
        "enabled": bool(cap.get("enabled", True)),
        "health": cap.get("health", "ok"),
        "registered_at": __import__("time").time(),
    }
    existing = c.kv_get(key)
    if isinstance(existing, list):
        # 同organ多能力: 追加(按name去重)
        existing = [x for x in existing if x.get("name") != entry["name"]]
        existing.append(entry)
        c.kv_set(key, existing)
    else:
        c.kv_set(key, [entry])
    return True, ""

def unregister(c, organ: str, name: str = None):
    """注销能力(插件卸载时). name=None注销该organ全部."""
    key = KV_NS + organ
    if name is None:
        c.kv_del(key if key else KV_NS + organ)
        return True
    caps = c.kv_get(key)
    if not isinstance(caps, list):
        return False
    caps = [x for x in caps if x.get("name") != name]
    if caps:
        c.kv_set(key, caps)
    else:
        c.kv_del(key)
    return True

def list_all(c) -> list:
    """拉取全部enabled能力(大loop匹配用). 返回扁平列表. 跳过learn_queue等非注册条目."""
    out = []
    store = c.kv_list(KV_NS)
    for k, caps in store.items():
        if k == KV_NS + "learn_queue":
            continue  # 待学清单不是能力声明
        if isinstance(caps, list):
            for cap in caps:
                if isinstance(cap, dict) and cap.get("enabled", True) and cap.get("organ") and cap.get("name"):
                    out.append(cap)
    return out

def to_prompt(c) -> str:
    """把能力注册表渲染成大loop匹配用的prompt段落."""
    caps = list_all(c)
    if not caps:
        return "(能力注册表为空)"
    lines = []
    for cap in caps:
        ex = ("例: " + "; ".join(cap["examples"][:2])) if cap.get("examples") else ""
        lines.append(f"- [{cap['organ']}/{cap['name']}] {cap['description']} {ex}")
    return "\n".join(lines)
