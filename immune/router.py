#!/usr/bin/env python3
"""免疫层 · 事件路由器
三级过滤: 白名单(类型直通) → 关键词(跨类型) → 挂起区(兜底, 反思周期扫)
设计: wikis/nono/core-architecture-v1.md §4
"""
import json, time, uuid
from state.model import Signal

# 事件路由表: 事件类型 → (优先级, 衰减率, 是否打扰主人)
ROUTE_TABLE = {
    "master_message": {"priority": 0, "decay": 0.01,  "to": "dialogue"},
    "risk_trigger":   {"priority": 0, "decay": 0.01,  "to": "master_alert"},
    "task_done":      {"priority": 2, "decay": 0.15, "to": "reflection"},
    "price_alert":    {"priority": 1, "decay": 0.1,  "to": "master_alert"},
    "heartbeat":      {"priority": 3, "decay": 0.3,  "to": "reflection"},
    "loop_progress":  {"priority": 3, "decay": 0.4,  "to": "state_only"},
    "loop_error":     {"priority": 1, "decay": 0.1,  "to": "reflection"},
    "task_forward":   {"priority": 1, "decay": 0.05, "to": "reflection"},
    "reflection_trigger": {"priority": 2, "decay": 0.05, "to": "reflection"},
}

# 器官协作v1: task_forward按payload里的target路由到对应器官(默认表项只做兜底)
TASK_TARGET_ROUTE = {
    "self_check": "reflection",   # 系统自检/修bug → reflection真正执行
    "reflection": "reflection",   # 需要思考/吸收/记录的任务 → reflection
    "reflect": "reflection",
}

# 跨类型关键词(内容语义补刀: 白名单外的信号如果命中也升级)
KEYWORD_ESCALATE = {
    "紧急": 0, "风险": 0, "错了": 0, "快": 1,
}

class Router:
    def __init__(self, client):
        self.broker = client

    def route(self, sig_type: str, payload: dict, source: str = "") -> Signal:
        """信号进入系统的唯一入口. 返回路由后的Signal(已带衰减率/优先级)"""
        # P2-3: key规范化(查找前转小写) + master_message必需字段text检查
        sig_type = (sig_type or "").lower()
        if sig_type == "master_message" and not (isinstance(payload, dict) and payload.get("text")):
            sig = Signal(sig_type=sig_type,
                         payload={"data": payload, "route_to": "suspended", "priority": 3},
                         strength=1.0, decay=0.3, source=source,
                         sig_id=uuid.uuid4().hex[:12])
            self.broker.kv_set(f"suspended/{sig.sig_id}",
                               {"type": sig_type, "payload": payload,
                                "at": time.time(), "ttl": 7*24*3600,
                                "reason": "master_message缺少必需字段text"})
            return sig
        rule = ROUTE_TABLE.get(sig_type)
        if rule is None:
            # 白名单外: 默认低优先级, 进挂起区(不丢!)
            rule = {"priority": 3, "decay": 0.3, "to": "suspended"}

        # task_forward: 按payload里的target动态路由(target→器官), 覆盖静态表项
        if sig_type == "task_forward" and isinstance(payload, dict):
            target = payload.get("target")
            if not isinstance(target, str) and isinstance(payload.get("data"), dict):
                target = payload["data"].get("target")
            organ = TASK_TARGET_ROUTE.get(target) if isinstance(target, str) else None
            if organ:
                rule = dict(rule, to=organ)

        # 内容关键词升级
        text = json.dumps(payload, ensure_ascii=False)
        for kw, pri in KEYWORD_ESCALATE.items():
            if kw in text and pri < rule["priority"]:
                rule = dict(rule, priority=pri, decay=0.0)

        sig = Signal(
            sig_type=sig_type,
            payload={"data": payload, "route_to": rule["to"], "priority": rule["priority"]},
            strength=1.0,
            decay=rule["decay"],
            source=source,
            sig_id=uuid.uuid4().hex[:12],
        )
        if rule["to"] == "suspended":
            # 挂起区修复: 只进kv(不双写signals), TTL 7天由sweep清理
            self.broker.kv_set(f"suspended/{sig.sig_id}",
                               {"type": sig_type, "payload": payload,
                                "at": time.time(), "ttl": 7*24*3600})
        else:
            self.broker.emit(sig.sig_type, sig.payload, strength=sig.strength,
                             decay=sig.decay, source=sig.source, sig_id=sig.sig_id)
        return sig
