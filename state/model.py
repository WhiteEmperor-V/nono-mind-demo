#!/usr/bin/env python3
"""Signal 单点定义(修复: 路由断链+双源分裂)
所有组件(signal发射者/路由器/意识核/服务)都从这里import, 严禁各自定义
"""
import time, uuid
from dataclasses import dataclass, field, asdict

# 优先级表(唯一权威, 路由器和意识核共用)
PRIORITY = {
    "master_message": 0, "risk_trigger": 0, "price_alert": 1,
    "task_done": 2, "loop_error": 1, "heartbeat": 3,
    "task_forward": 1, "loop_progress": 3, "test_signal": 3, "decay_test": 3,
    "die_test": 3, "focus_test": 3, "concurrent": 3,
}

# P0信号(主人消息/风险)永不清除, 但有TTL(秒)强制过期
P0_TTL = 24 * 3600

# clamp边界: 服务端强制(防信号劫持)
STRENGTH_MAX = 10.0
DECAY_MIN = 0.01   # 即使P0也有微量衰减(除非走TTL通道)


@dataclass
class Signal:
    sig_type: str
    payload: dict
    strength: float = 1.0
    decay: float = 0.3
    created: float = field(default_factory=time.time)
    source: str = ""
    sig_id: str = ""

    def __post_init__(self):
        if not self.sig_id:
            self.sig_id = uuid.uuid4().hex[:12]
        # 全域clamp(修复: decay=0永不清除 + 信号劫持)
        self.strength = max(0.0, min(self.strength, STRENGTH_MAX))
        self.decay = max(DECAY_MIN, min(self.decay, 0.95))
        # P0信号用TTL代替永久驻留: decay给最小值, TTL由服务端按created+P0_TTL判定
        if self.sig_type in PRIORITY and PRIORITY[self.sig_type] == 0:
            self.decay = DECAY_MIN

    def to_row(self):
        return (self.sig_id, self.sig_type, self.payload_json(),
                self.strength, self.decay, self.created, self.source)

    def payload_json(self):
        import json
        return json.dumps(self.payload, ensure_ascii=False)

    @classmethod
    def from_row(cls, row):
        import json
        return cls(sig_type=row["sig_type"],
                   payload=json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
                   strength=row["strength"], decay=row["decay"],
                   created=row["created"], source=row["source"], sig_id=row["sig_id"])

    def expired(self) -> bool:
        """P0信号TTL强制过期; 普通信号按强度(服务端tick衰减)"""
        if self.sig_type in PRIORITY and PRIORITY[self.sig_type] == 0:
            return time.time() - self.created > P0_TTL
        return False
