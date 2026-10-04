#!/usr/bin/env python3
"""judgments 判断账: 她"做过的判断"留痕 + 每日失效探测 + 换脑重验.

解决Tibo那个问题: 判断"存了不代表还对"——换脑(模型)或时间过去后旧判断可能是错的.
三挂点(全寄生现有机制, 不新增进程/库/依赖, 纯 state.db 的 judgments/ 命名空间KV):
  A 写判断   —— core/loop.py 做架构级决策时调 write_judgment(见 core/loop.note_decision)
  B 每日探测 —— wellness.py 每自然日调 maybe_daily_probe: 拿 invalid_if 真探测, 失效→void
  C 换脑重验 —— llm_client.py 检测脑子名变化后调 note_brain: 问新脑子逐条复查, 不成立/拿不准→recheck
ponytail: 没有探测函数的纯主观判断 → 跳过, 不瞎标status(宁可不动).
"""
import hashlib
import os
import time

JUDG_NS = "judgments/"                 # 判断账命名空间(跟 reflections/ 平级)
BRAIN_KV = "llm/current_brain"         # 当前脑子名(哪个provider/model在响应)
REVIEW_KV = "judgment_reviews"         # 换脑重验留痕(append)
LAST_PROBE_KV = "wellness/last_judgment_probe_day"  # 每日探测节流(挂在wellness下, 别污染judgments/)


def _jid(text: str) -> str:
    """按判断正文派生稳定id: 同一条判断重复写=覆盖, 防 judgments/ 爆炸(ponytail)."""
    return hashlib.md5(text.strip().encode("utf-8")).hexdigest()[:12]


def current_brain(c) -> str:
    return (c.kv_get(BRAIN_KV) or {}).get("brain", "")


# ---------------- 挂点A: 写判断 ----------------
def write_judgment(c, text, why="", invalid_if="", probe="", probe_arg=None, brain="", jid=None):
    """写/更新一条judgment到 judgments/<id>(一judgment=一KV).
    判据(由调用方把关): 只在决策动了 llm_client通道/state.db配置/放弃可选方案之一时才写, 防爆炸.
    同正文重复写=覆盖同一条(保留created), 不产生重复条目."""
    jid = jid or _jid(text)
    key = JUDG_NS + jid
    prev = c.kv_get(key) or {}
    rec = {
        "text": text,
        "why": why,
        "invalid_if": invalid_if,
        "probe": probe,
        "brain": brain or current_brain(c),
        "status": "active",
        "created": prev.get("created", time.time()),
        "verified_at": time.time(),
    }
    if probe_arg is not None:
        rec["probe_arg"] = probe_arg
    c.kv_set(key, rec)
    return jid


# ---------------- 挂点B: 每日失效探测 ----------------
def probe_scnet_alive(j) -> bool:
    """invalid_if='scnet通道恢复': scnet能回话=条件成立(旧判断作废)→True.
    真发个最小请求试scnet通没通(复用llm_client现有降级链的单通道, 不新开通道)."""
    try:
        import llm_client as L
        L._try_chain([{"role": "user", "content": "回复OK"}], 5, L._chat_chain()[:1], timeout=10)
        return True      # scnet回了 = 恢复 = 该判断作废
    except Exception:
        return False     # scnet没通 = 判断仍成立 = 保持active(verified_at仍会刷新)


def probe_file_exists(j) -> bool:
    """invalid_if='某文件还存在': 文件真在=条件成立→True. 路径由 judge 的 probe_arg 指定."""
    p = (j or {}).get("probe_arg")
    return bool(p) and os.path.exists(p)


# 探测函数表: 名字 -> fn(judgment)->bool(True=invalid_if条件成立=该判断作废)
PROBES = {
    "probe_scnet_alive": probe_scnet_alive,
    "probe_file_exists": probe_file_exists,
}


def probe_all(c) -> dict:
    """对 judgments/ 里每条 status=active 且配了探测函数的判断做实际探测.
    条件成立→status=void; 不成立→刷新verified_at保持active;
    没探测函数(纯主观)/探测异常 → 跳过, 不动status(不瞎标). 返回统计."""
    res = {"checked": 0, "voided": 0, "skipped": 0}
    for key, j in (c.kv_list(JUDG_NS) or {}).items():
        if not isinstance(j, dict) or j.get("status") != "active":
            continue
        probe = j.get("probe")
        fn = PROBES.get(probe) if probe else None
        if fn is None:
            res["skipped"] += 1
            continue
        try:
            invalid = bool(fn(j))
        except Exception as e:
            print(f"[judgments] 探测{probe}异常(跳过不动status): {e}")
            res["skipped"] += 1
            continue
        j["verified_at"] = time.time()
        res["checked"] += 1
        if invalid:
            j["status"] = "void"
            res["voided"] += 1
            print(f"[judgments] 判断已失效(void): {j.get('text', '')[:40]} (probe={probe})")
        c.kv_set(key, j)
    return res


def maybe_daily_probe(c, today=None) -> dict:
    """每自然日只探测一次(省资源, 不跟wellness的5min同频). 今天已探过→返回None."""
    today = today or time.strftime("%Y-%m-%d")
    if (c.kv_get(LAST_PROBE_KV) or {}).get("day") == today:
        return None
    res = probe_all(c)
    c.kv_set(LAST_PROBE_KV, {"day": today, "at": time.time()})
    return res


# ---------------- 挂点C: 换脑重验 ----------------
def _ask_brain(j, new_brain="") -> str:
    """把一条judgment发给当前(新)脑子过一遍: 只回 成立/不成立/拿不准. 走现有降级链."""
    from llm_client import chat
    prompt = (f"你现在的脑子是 {new_brain or '当前模型'}。下面这条判断是以前用别的脑子做的, "
              "它现在还成立吗? 只回三个词之一: 成立 / 不成立 / 拿不准。\n"
              f"判断: {j.get('text', '')}\n依据: {j.get('why', '')}")
    return chat([{"role": "user", "content": prompt}], max_tokens=8)


def reverify_active(c, new_brain, ask=None) -> dict:
    """换脑时对每条 status=active 的判断问新脑子复查.
    回"成立"→保持active; "不成立"/"拿不准"→status=recheck(标待主人定夺, 不自动扔). 结果落judgment_reviews."""
    ask = ask or _ask_brain
    res = {"reviewed": 0, "recheck": 0}
    for key, j in (c.kv_list(JUDG_NS) or {}).items():
        if not isinstance(j, dict) or j.get("status") != "active":
            continue
        try:
            verdict = ask(j, new_brain)
        except Exception as e:
            print(f"[judgments] 问新脑子失败(跳过): {e}")
            continue
        res["reviewed"] += 1
        st = "active" if str(verdict).strip().startswith("成立") else "recheck"
        j["status"] = st
        j["verified_at"] = time.time()
        c.kv_set(key, j)
        c.kv_append(REVIEW_KV, {"at": time.time(), "key": key,
                                "text": str(j.get("text", ""))[:120],
                                "brain": new_brain, "verdict": str(verdict)[:20]})
        if st == "recheck":
            res["recheck"] += 1
    return res


def note_brain(c, used, ask=None) -> bool:
    """挂点C入口(llm_client每次响应后调): 记录当前脑子名; 与上次不同→跑一次换脑重验.
    返回是否发生了换脑. 首次记录(无prev)不算换脑(不误触发)."""
    prev = (c.kv_get(BRAIN_KV) or {}).get("brain")
    c.kv_set(BRAIN_KV, {"brain": used, "at": time.time()})
    if prev and prev != used:
        reverify_active(c, used, ask=ask)
        return True
    return False
