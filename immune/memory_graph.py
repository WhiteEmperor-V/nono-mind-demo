#!/usr/bin/env python3
"""记忆关系图谱(NOOA式): 知识记录间的关系(supports/contradicts/derived_from) + agent自管记忆
NOOA启发: 记忆不是平铺日志, 是图谱——检索时顺藤摸瓜(从一条记忆跳到它的邻居)。
写入治理: agent对话中自主决定"值得记"→memory_write; 反思pass负责合并/蒸馏/剪枝(已有reflection)。
"""
import json, time, uuid

VALID_RELS = ("supports", "contradicts", "derived_from", "relates_to")

def add_relation(c, src_key: str, rel: str, dst_key: str, note: str = ""):
    """给两条知识记录加关系边. 双向可见(关系存两边记录的relations字段)"""
    if rel not in VALID_RELS:
        raise ValueError(f"非法关系: {rel}, 必须是{VALID_RELS}")
    if src_key == dst_key:
        raise ValueError("自指关系无意义")
    edge = {"rel": rel, "other": dst_key, "note": note[:100], "at": time.time()}
    for key, e in ((src_key, edge), (dst_key, {**edge, "other": src_key})):
        rec = c.kv_get(key)
        if not isinstance(rec, dict):
            continue  # 目标记录不存在就不硬塞
        rels = rec.get("relations", [])
        rels.append(e)
        rec["relations"] = rels[-20:]  # 防膨胀: 每记录最多20条边
        c.kv_set(key, rec)
    return edge

def neighbors(c, key: str, depth: int = 1) -> list:
    """顺藤摸瓜: 从一条记忆出发拿它的邻居记录(带关系说明)"""
    seen, frontier, out = {key}, [key], []
    for _ in range(depth):
        nxt = []
        for k in frontier:
            rec = c.kv_get(k)
            if not isinstance(rec, dict):
                continue
            for e in rec.get("relations", []):
                other = e.get("other")
                if other and other not in seen:
                    seen.add(other)
                    nxt.append(other)
                    orec = c.kv_get(other) or {}
                    text = str(orec.get("note") or orec.get("content") or orec.get("result") or "")[:120]
                    out.append({"rel": e.get("rel"), "key": other, "content": text,
                                "confidence": orec.get("confidence", "AMBIGUOUS")})
        frontier = nxt
    return out

def memory_write(c, note: str, importance: int = 5, tags: list = None, confidence: str = "INFERRED") -> str:
    """agent自管记忆: 对话中agent自主决定"这条值得记"(NOOA式curate, 不等反思)"""
    from immune import trust
    mid = f"memories/{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    rec = trust.stamp({
        "at": time.time(), "note": note[:500], "importance": max(1, min(10, int(importance))),
        "tags": list(tags or [])[:8], "source": "agent_self"}, confidence)
    c.kv_set(mid, rec)
    return mid

def memory_query(c, query: str, top_k: int = 3) -> list:
    """agent自管记忆查询: 关键词三因子简化版(复用retrieval的打分)"""
    from immune.retrieval import search
    return search(c, query, namespaces=("memories/",), top_k=top_k)
