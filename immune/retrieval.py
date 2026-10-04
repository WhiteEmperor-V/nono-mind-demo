#!/usr/bin/env python3
"""地基③+: 三因子检索(recency + importance + keyword relevance)
Generative Agents理论: score = w1·recency + w2·importance + w3·relevance
简化实现(无embedding): 关键词重合度替代relevance——够用且零依赖
用例: dialogue/大loop回答"刚才聊的/之前做的"时从insights/procedures检索
"""
import time, re

def _recency(ts, halflife_hours=24):
    """指数衰减: 半衰期24小时"""
    age_h = max(0.0, (time.time() - ts) / 3600.0)
    return 0.5 ** (age_h / halflife_hours)

def _keywords(text: str):
    """中文按2-gram+英文按词切(简易关键词)"""
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text.lower())
    words = set()
    for w in text.split():
        if len(w) >= 2:
            words.add(w)
        # 中文2-gram
        cn = re.findall(r"[\u4e00-\u9fff]{2,}", w)
        for seg in cn:
            for i in range(len(seg) - 1):
                words.add(seg[i:i+2])
    return words

def _relevance(query_words: set, item_text: str) -> float:
    item_words = _keywords(item_text)
    if not item_words:
        return 0.0
    overlap = len(query_words & item_words)
    return overlap / max(len(query_words), 1)

def search(c, query: str, namespaces=("insights/", "procedures/", "memories/"), top_k=3) -> list:
    """三因子检索+图谱扩散(NOOA顺藤摸瓜). 返回[{key, content, score, confidence}]按分数降序"""
    q_words = _keywords(query)
    results = []
    now = time.time()
    for ns in namespaces:
        store = c.kv_list(ns)
        for k, v in store.items():
            if not isinstance(v, dict):
                continue
            ts = v.get("at") or v.get("updated_at") or 0
            text = str(v.get("note") or v.get("result") or v.get("demand") or "")[:300]
            importance = float(v.get("importance", 3))
            rec = _recency(ts)
            rel = _relevance(q_words, text + " " + k)
            # 归一化加权(理论: Generative Agents等权起步, 实测再调)
            score = 0.3 * rec + 0.4 * (importance / 10.0) + 0.3 * rel
            if rel > 0 or rec > 0.1:
                results.append({"key": k, "content": text[:200], "score": round(score, 3),
                                "confidence": v.get("confidence", "AMBIGUOUS")})
    results.sort(key=lambda x: -x["score"])
    results = results[:top_k]
    # 图谱扩散: 命中记录的关系邻居一并带上(降0.5分, 非直接命中但有上下文价值)
    from immune import memory_graph as _mg
    seen = {r["key"] for r in results}
    extras = []
    for r in results:
        for nb in _mg.neighbors(c, r["key"], depth=1):
            if nb["key"] not in seen:
                seen.add(nb["key"])
                extras.append({**nb, "score": round(r["score"] * 0.5, 3), "via": r["key"]})
    results.extend(sorted(extras, key=lambda x: -x["score"])[:top_k])
    return results

def to_prompt(c, query: str, top_k=3) -> str:
    """渲染成prompt段落(无相关内容返回空串). 置信度标注走immune/trust"""
    hits = search(c, query, top_k=top_k)
    if not hits:
        return ""
    from immune import trust
    return trust.annotate_prompt(hits)
