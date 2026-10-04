#!/usr/bin/env python3
"""信任标签(Graphify三级置信度): 认知库每条记录带confidence
EXTRACTED=有实证(事件日志/测试结果/代码验证) INFERRED=推断(反思/归纳) AMBIGUOUS=不确定(待验证)
铁律: 检索时AMBIGUOUS必须在prompt里标"不确定", 不许冒充实证知识。
"""
import time

VALID = ("EXTRACTED", "INFERRED", "AMBIGUOUS")
# 认知库命名空间(哪些kv前缀算"知识记录")
KNOWLEDGE_NS = ("insights/", "procedures/", "coder/knowledge/")

def stamp(record: dict, confidence: str, evidence: str = "") -> dict:
    """给知识记录打信任标签(写入时调用). 无效标签拒绝"""
    if confidence not in VALID:
        raise ValueError(f"非法confidence: {confidence}, 必须是{VALID}")
    record["confidence"] = confidence
    if evidence:
        record["evidence"] = str(evidence)[:200]
    record["stamped_at"] = time.time()
    return record

def of(key: str, record: dict) -> str:
    """读记录的置信度(老数据无标签=AMBIGUOUS诚实兜底)"""
    if key.startswith(KNOWLEDGE_NS):
        c = record.get("confidence") if isinstance(record, dict) else None
        return c if c in VALID else "AMBIGUOUS"
    return "EXTRACTED"  # 非知识库(状态/任务)默认实证级

def annotate_prompt(hits: list) -> str:
    """检索结果渲染时按置信度标注(Graphify: 你永远知道哪些是找到的哪些是猜的)"""
    lines = []
    for h in hits:
        key, content = h.get("key", ""), h.get("content", "")
        conf = of(key, h)
        tag = {"EXTRACTED": "", "INFERRED": "(推断)", "AMBIGUOUS": "(⚠️不确定,待验证)"}[conf]
        lines.append(f"- [{key.split('/')[-1]}]{tag} {content}")
    return "相关记忆:\n" + "\n".join(lines)
