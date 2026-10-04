import sys, os, re
sys.path.insert(0, "/root/nono-mind")
os.chdir("/root/nono-mind")

class MemState:
    def __init__(self): self.d = {}
    def kv_set(self, k, v): self.d[k] = v
    def kv_get(self, k): return self.d.get(k)
    def kv_list(self, p=""):
        return {k: v for k, v in self.d.items() if k.startswith(p)} if p else {}
    def kv_del(self, k): self.d.pop(k, None)
    def get_history_range(self, days=1): return []
    def kv_append(self, k, v): pass

c = MemState()
from immune.capabilities import register
for cap in [
    {"organ": "dialogue", "name": "chat", "description": "日常聊天对话", "when_to_use": "主人闲聊", "when_not": "任务类", "params_schema": {}, "examples": ["你好"]},
    {"organ": "canvas", "name": "create", "description": "画布创作", "when_to_use": "要画画", "when_not": "文字任务", "params_schema": {}, "examples": ["画个图"]},
    {"organ": "coder", "name": "code", "description": "编程任务", "when_to_use": "写代码", "when_not": "闲聊", "params_schema": {}, "examples": ["写脚本"]},
]:
    register(c, cap)
from immune.core_memory import seed_defaults
seed_defaults(c)

from organs.dialogue import _build_self_awareness
from immune.capabilities import to_prompt
from immune.core_memory import read_core

persona = open("organs/dialogue.py").read()
m = re.search(r'PERSONA = """(.*?)"""', persona, re.S)
persona_len = len(m.group(1)) if m else 0
aware = _build_self_awareness(c)
core = read_core(c)
caps = to_prompt(c)
total = persona_len + len(aware) + len(core) + len(caps)
print(f"PERSONA: {persona_len}字")
print(f"自我认知(含状态快照): {len(aware)}字")
print(f"core memory: {len(core)}字")
print(f"能力注册表: {len(caps)}字")
print(f"SYSTEM总量: ~{total}字 ≈ {total//2}token (中文≈2字/token)")
