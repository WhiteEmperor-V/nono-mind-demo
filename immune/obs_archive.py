#!/usr/bin/env python3
"""SoL-Pi式输出归档与句柄(ObservationPack同构) + 证据核验(对照事件日志)
架构约束(主人9/12定): 这些是器官内部的纯函数工具——不新增任何器官间自主通讯,
通讯决策权始终在核心loop。器官只是把大输出换成句柄, 取回动作也由核心loop触发的执行流完成。

- archive_output: 长输出归档本地 + 返回句柄(短), 上下文只带句柄
- fetch_handle: 按句柄分页取回(核心loop执行流需要细节时调用)
- verify_claims: 诊断回执逐条对照原始证据核验(只放行对得上的, SoL-Pi Evidence-Preserving Reducer)
"""
import json, os, time, re

ARCHIVE_DIR = "/root/nono-mind/obs_archive"
HANDLE_MAX = 200      # 句柄里保留的摘要长度
PAGE = 1000           # 取回一页的字符数

def archive_output(content: str, tag: str = "obs") -> dict:
    """长输出归档. 返回{handle, excerpt, full_len, path}
    短输出(<=HANDLE_MAX)不归档, 直接原样返回(handle=None)"""
    if len(content) <= HANDLE_MAX:
        return {"handle": None, "excerpt": content, "full_len": len(content)}
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    hid = f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid() % 10000:04d}"
    path = os.path.join(ARCHIVE_DIR, f"{hid}.txt")
    with open(path, "w") as f:
        f.write(content)
    return {"handle": hid, "excerpt": content[:HANDLE_MAX], "full_len": len(content), "path": path}

def fetch_handle(handle: str, page: int = 1) -> str:
    """按句柄取回第page页(PAGE字符/页). 越界返回空"""
    if not re.fullmatch(r"[a-z0-9_]+", handle or ""):
        return "(非法句柄)"
    path = os.path.join(ARCHIVE_DIR, f"{handle}.txt")
    if not os.path.exists(path):
        return "(句柄不存在或已过期)"
    src = open(path, encoding="utf-8", errors="ignore").read()
    start = (page - 1) * PAGE
    chunk = src[start:start + PAGE]
    if not chunk:
        return "(无更多内容)"
    total_pages = (len(src) + PAGE - 1) // PAGE
    return f"[{handle} 第{page}/{total_pages}页]\n{chunk}"

def verify_claims(claims: list, evidence_texts: list) -> dict:
    """证据核验(SoL-Pi Evidence-Preserving Reducer): 逐条claim对照evidence原文,
    只放行能在原文中找到实质依据的claim. 返回{verified: [...], rejected: [...]}
    核验规则: claim中的关键片段(>=6字符连续匹配, 忽略空白差异)必须出现在某条evidence里"""
    ev_norm = [re.sub(r"\s+", "", e) for e in evidence_texts]
    verified, rejected = [], []
    for claim in claims:
        c_norm = re.sub(r"\s+", "", claim)
        # 抽claim的关键片段: 按标点切分, 取>=8字符的最长片段核验
        frags = [f for f in re.split(r"[,，。;；:：!！?？\n]", claim) if len(re.sub(r"\s+", "", f)) >= 8]
        if not frags:
            rejected.append({"claim": claim, "why": "片段太短无法核验"})
            continue
        ok = any(any(frag_norm in ev for ev in ev_norm)
                 for frag in frags
                 for frag_norm in [re.sub(r"\s+", "", frag)])
        (verified if ok else rejected).append(
            {"claim": claim} if ok else {"claim": claim, "why": "原文中找不到依据"})
    return {"verified": verified, "rejected": rejected}
