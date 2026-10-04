#!/usr/bin/env python3
"""memories/蒸馏(NOOA reflection pass的Python版): 合并重复/剪枝过期/保持图谱边
reflection闲时调用。规则(Ponytail: 确定性规则优先, 不花LLM):
- 相似度>0.8的两条 → 合并(importance取高, 边并入)
- importance<=2且超30天 → 剪枝(删除)
- 边指向已删记录 → 清理死边
"""
import time, re

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text))

def _similar(a: str, b: str) -> float:
    """字符2-gram Jaccard相似度(零依赖)"""
    def grams(t):
        t = _norm(t)
        return {t[i:i+2] for i in range(len(t) - 1)} if len(t) >= 2 else {t}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)

def consolidate(c, dry_run: bool = False) -> dict:
    """跑一轮memories/蒸馏. 返回统计"""
    store = c.kv_list("memories/")
    keys = sorted(store.keys())
    merged, pruned, dead_edges = 0, 0, 0
    dead = set()
    now = time.time()

    # 1. 合并重复(相似度>0.8, 保importance高的, 边并入)
    for i, ka in enumerate(keys):
        if ka in dead:
            continue
        va = store[ka]
        if not isinstance(va, dict):
            continue
        for kb in keys[i+1:]:
            if kb in dead:
                continue
            vb = store[kb]
            if not isinstance(vb, dict):
                continue
            if _similar(va.get("note", ""), vb.get("note", "")) > 0.8:
                # 保importance高的
                keep, drop = (ka, kb) if va.get("importance", 0) >= vb.get("importance", 0) else (kb, ka)
                vkeep, vdrop = store[keep], store[drop]
                # 边并入(去重)
                rels = vkeep.get("relations", [])
                have = {(e.get("rel"), e.get("other")) for e in rels}
                for e in vdrop.get("relations", []):
                    if (e.get("rel"), e.get("other")) not in have:
                        rels.append(e)
                vkeep["relations"] = rels[-20:]
                vkeep["merged_from"] = vkeep.get("merged_from", []) + [drop]
                if not dry_run:
                    c.kv_set(keep, vkeep)
                    c.kv_del(drop)
                dead.add(drop)
                merged += 1

    # 2. 剪枝: importance<=2 且超30天
    for k, v in store.items():
        if k in dead or not isinstance(v, dict):
            continue
        if v.get("importance", 5) <= 2 and (now - v.get("at", now)) > 30 * 86400:
            if not dry_run:
                c.kv_del(k)
            dead.add(k)
            pruned += 1

    # 3. 清死边(边指向已删/不存在的记录)
    for k, v in store.items():
        if k in dead or not isinstance(v, dict):
            continue
        rels = v.get("relations", [])
        alive = [e for e in rels if e.get("other") not in dead and c.kv_get(e.get("other")) is not None]
        if len(alive) != len(rels):
            dead_edges += len(rels) - len(alive)
            v["relations"] = alive
            if not dry_run:
                c.kv_set(k, v)

    return {"total": len(store), "merged": merged, "pruned": pruned, "dead_edges": dead_edges}

if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/root/nono-mind")
    from state.server import StateClient
    c = StateClient()
    print("dry_run:", consolidate(c, dry_run=True))
    print("实跑:", consolidate(c))
    c.close()
